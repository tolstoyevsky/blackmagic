#!/usr/bin/env python3
import logging
import os
import os.path

import tornado.web
import tornado.options
from cdtz import set_time_zone
from debian import deb822
from pymongo import MongoClient
from shirow.ioloop import IOLoop
from shirow.server import RPCServer, TOKEN_PATTERN, remote
from tornado.options import define, options

from blackmagic import defaults, docker
from blackmagic.db import Image
from blackmagic.codes import LOCKED, READY
from blackmagic.decorators import only_if_initialized

define('base_systems_path',
       default='/var/chroot',
       help='The path to the directory which contains chroot environments '
            'which, in turn, contain the Debian base system')
define('db_name',
       default='cusdeb',
       help='')
define('dominion_workspace',
       default='/var/dominion/workspace/',
       help='')
define('max_builds_number',
       default=8,
       type=int,
       help='Maximum allowed number of builds at the same time.')
define('mongodb_host',
       default='',
       help='')
define('mongodb_port',
       default='33018',
       help='')

LOGGER = logging.getLogger('tornado.application')


class DistroDoesNotExist(Exception):
    """Exception raised by the get_os_name function if the specified suite is not valid. """


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/' + TOKEN_PATTERN, RPCHandler),
        ]
        super().__init__(handlers)


class RPCHandler(RPCServer):
    base_packages_list = {}
    users_list = {}

    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)

        self._global_lock = True
        self._init_lock = False

        self._collection = None
        self._collection_name = ''
        self._db = None

        self._distro = None
        self._target_device = None

        self._base_packages_number = 0
        self._base_packages_query = {}
        self._selected_packages = []

        self._image = None

        self._user = None  # the one who builds an image

    def destroy(self):
        self._image.dump_sync()

    def _init_mongodb(self):
        client = MongoClient(options.mongodb_host, int(options.mongodb_port))
        self._db = client[options.db_name]

    @remote
    async def init(self, request, name, device_name, distro_name, flavour):
        if self._init_lock:
            request.ret(LOCKED)

        self._init_lock = True

        self._image = Image(self.user_id, name, device_name, distro_name, flavour)

        self._init_mongodb()
        self._collection_name = distro_name
        self._collection = self._db[self._collection_name]

        self._base_packages_query = {
            'package': {
                '$in': self.base_packages_list[self._collection_name],
            },
        }
        self._base_packages_number = self._collection.find(self._base_packages_query).count()

        LOGGER.debug('Finishing initialization')

        self._init_lock = False
        self._global_lock = False

        request.ret_and_continue(self._image.image_id)

        request.ret(READY)

    @only_if_initialized
    @remote
    async def build(self, request):
        self._image.enqueue()
        await self._image.dump()

        request.ret(READY)

    @only_if_initialized
    @remote
    async def add_user(self, request, username, password, uid, gid, comment, homedir, shell):
        request.ret(READY)

    @only_if_initialized
    @remote
    async def change_root_password(self, request, password):
        request.ret(READY)

    @only_if_initialized
    @remote
    async def sync_configuration(self, request, image_configuration_params):
        request.ret(READY)

    @only_if_initialized
    @remote
    async def get_default_configuration(self, request):
        request.ret(defaults.CONFIGURATION)

    @only_if_initialized
    @remote
    async def get_packages_list(self, request, page_number, per_page, search_token=None):
        if page_number > 0:
            start_position = (page_number - 1) * per_page
        else:
            start_position = 0

        find_query = {}
        if search_token:
            find_query.update({
                'package': {'$regex': search_token},
            })

        packages_list = []
        for document in self._collection.find(find_query).skip(start_position).limit(per_page):
            # Originally _id is an ObjectId instance and it's not JSON serializable
            document['_id'] = str(document['_id'])

            if document['package'] in self.base_packages_list[self._collection_name]:
                document['type'] = 'base'
            if document['package'] in self._selected_packages:
                document['type'] = 'selected'

            packages_list.append(document)

        request.ret(packages_list)

    @only_if_initialized
    @remote
    async def get_base_packages_list(self, request, page_number, per_page):
        start_position = (page_number - 1) * per_page if page_number > 0 else 0

        collection = self._collection
        base_packages_list = []
        for document in collection.find(
                self._base_packages_query
        ).skip(start_position).limit(per_page):
            # Originally _id is an ObjectId instance and it's not JSON serializable
            document['_id'] = str(document['_id'])
            base_packages_list.append(document)

        request.ret(base_packages_list)

    @only_if_initialized
    @remote
    async def get_selected_packages_list(self, request, page_number, per_page):
        start_position = (page_number - 1) * per_page if page_number > 0 else 0

        collection = self._collection
        selected_packages_list = []
        for document in collection.find({
            'package': {
                '$in': self._selected_packages,
            }
        }).skip(start_position).limit(per_page):
            # Originally _id is an ObjectId instance and it's not JSON serializable
            document['_id'] = str(document['_id'])
            selected_packages_list.append(document)

        request.ret(selected_packages_list)

    @only_if_initialized
    @remote
    async def get_default_root_password(self, request):
        request.ret(defaults.ROOT_PASSWORD)

    @only_if_initialized
    @remote
    async def get_shells_list(self, request):
        request.ret(['/bin/sh', '/bin/dash', '/bin/bash', '/bin/rbash'])

    @only_if_initialized
    @remote
    async def get_packages_number(self, request, search_token=None):
        find_query = {}
        if search_token:
            find_query.update({
                'package': {'$regex': search_token}
            })

        packages_number = self._collection.find(find_query).count()
        request.ret(packages_number)

    @only_if_initialized
    @remote
    async def get_base_packages_number(self, request):
        request.ret(self._base_packages_number)

    @only_if_initialized
    @remote
    async def get_selected_packages_number(self, request):
        selected_packages_count = self._collection.find({
            'package': {
                '$in': self._selected_packages,
            }
        }).count()
        request.ret(selected_packages_count)

    @only_if_initialized
    @remote
    async def get_users_list(self, request):
        request.ret(self.users_list[self._collection_name])

    @only_if_initialized
    @remote
    async def search(self, request, query):
        packages_list = []
        if query:
            matches = self._db.command('text', self._collection_name, search=query)
            if matches['results']:
                for document in matches['results']:
                    document['obj'].pop('_id')
                    packages_list.append(document['obj'])

        request.ret(packages_list)

    @only_if_initialized
    @remote
    async def resolve(self, request, packages_list):
        LOGGER.debug(f'Resolve dependencies for {packages_list}')
        self._selected_packages = packages_list
        request.ret([])


def main():
    set_time_zone(docker.TIME_ZONE)

    tornado.options.parse_command_line()
    if not os.path.isdir(options.base_systems_path):
        LOGGER.error('The directory specified via the base_systems_path parameter does not exist')
        exit(1)

    for item_name in os.listdir(options.base_systems_path):
        item_path = os.path.join(options.base_systems_path, item_name)
        if os.path.isdir(item_path):
            try:
                status_file = os.path.join(item_path, 'var/lib/dpkg/status')
                with open(status_file, encoding='utf-8') as infile:
                    RPCHandler.base_packages_list[item_name] = []
                    for package in deb822.Packages.iter_paragraphs(infile):
                        RPCHandler.base_packages_list[item_name].append(package['package'])
            except FileNotFoundError:
                LOGGER.info(f'{item_name} is not a Debian-based system')
                continue

            passwd_file = os.path.join(item_path, 'etc/passwd')
            with open(passwd_file, encoding='utf-8') as infile:
                RPCHandler.users_list[item_name] = []
                for line in infile:
                    RPCHandler.users_list[item_name].append(line.split(':'))

    LOGGER.info('RPC server is ready!')

    IOLoop().start(Application(), options.port)


if __name__ == "__main__":
    main()
