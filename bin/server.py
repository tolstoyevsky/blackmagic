#!/usr/bin/env python3
import logging
import os
import os.path

import tornado.web
import tornado.options
from appleseed import AlpineIndexFile, DebianIndexFile
from cdtz import set_time_zone
from motor import MotorClient
from shirow.ioloop import IOLoop
from shirow.server import RPCServer, TOKEN_PATTERN, remote
from tornado.options import define, options

from blackmagic import defaults, docker
from blackmagic.db import Image
from blackmagic.codes import (
    IMAGE_IS_NOT_AVAILABLE_FOR_RECOVERY,
    LOCKED,
    READY,
    RECOVERY_IMAGE_MISSING,
)
from blackmagic.decorators import only_if_initialized
from blackmagic.exceptions import RecoveryImageIsMissing
from images.models import Image as ImageModel
from images.serializers import ImageSerializer

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
            (r'/bm/token/' + TOKEN_PATTERN, RPCHandler),
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

        self._configuration = dict(defaults.CONFIGURATION)

        self._image = None
        self._need_update = True

        self._user = None  # the one who builds an image

    def destroy(self):
        if self._need_update and self._image:
            self._image.dump_sync()

    def _init_mongodb(self):
        client = MotorClient(options.mongodb_host, int(options.mongodb_port))
        self._db = client[options.db_name]

    async def _init(self, request, image_id=None, device_name=None, distro_name=None, flavour=None):
        if self._init_lock:
            request.ret(LOCKED)

        self._init_lock = True

        try:
            self._image = Image(image_id=image_id, user_id=self.user_id, device_name=device_name,
                                distro_name=distro_name, flavour=flavour)
        except RecoveryImageIsMissing:
            request.ret(RECOVERY_IMAGE_MISSING)

        if image_id:
            self._selected_packages = self._image.selected_packages
            self._configuration = self._image.configuration

        self._init_mongodb()
        self._collection_name = self._image.distro_name
        self._collection = self._db[self._collection_name]

        self._base_packages_query = {
            'package': {
                '$in': self.base_packages_list[self._collection_name],
            },
        }
        self._base_packages_number = await self._collection.count_documents(self._base_packages_query)

        LOGGER.debug('Finishing initialization')

        self._init_lock = False
        self._global_lock = False

    @remote
    async def init_new_image(self, request, device_name, distro_name, flavour):
        await self._init(request, device_name=device_name, distro_name=distro_name, flavour=flavour)

        request.ret_and_continue(self._image.image_id)
        request.ret(READY)

    @remote
    async def init_existing_image(self, request, image_id):
        await self._init(request, image_id=image_id)

        request.ret(READY)

    @remote
    async def is_image_available_for_recovery(self, request, image_id):
        try:
            image = ImageModel.objects.get(image_id=image_id, status=ImageModel.UNDEFINED)
            serializer = ImageSerializer(image)
            request.ret(serializer.data)
        except ImageModel.DoesNotExist:
            request.ret_error(IMAGE_IS_NOT_AVAILABLE_FOR_RECOVERY)

    @only_if_initialized
    @remote
    async def build(self, request):
        self._image.enqueue()
        await self._image.dump()

        self._need_update = False

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
    async def get_configuration(self, request):
        request.ret(self._configuration)

    @only_if_initialized
    @remote
    async def set_configuration(self, request, configuration):
        for key in configuration:
            if key in self._configuration:
                self._configuration[key] = configuration[key]

        self._image.configuration = self._configuration

        request.ret(READY)

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
                'package': {'$regex': search_token, '$options': '-i'},
            })

        packages_list = []
        async for document in self._collection.find(find_query).skip(start_position).limit(per_page):
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
        async for document in collection.find(
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
        async for document in collection.find({
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
                'package': {'$regex': search_token, '$options': '-i'}
            })

        packages_number = await self._collection.count_documents(find_query)
        request.ret(packages_number)

    @only_if_initialized
    @remote
    async def get_base_packages_number(self, request):
        request.ret(self._base_packages_number)

    @only_if_initialized
    @remote
    async def get_selected_packages_number(self, request):
        selected_packages_count = await self._collection.count_documents({
            'package': {
                '$in': self._selected_packages,
            }
        })
        request.ret(selected_packages_count)

    @only_if_initialized
    @remote
    async def get_users_list(self, request):
        request.ret(self.users_list[self._collection_name])

    @only_if_initialized
    @remote
    async def resolve(self, request, packages_list):
        LOGGER.debug(f'Resolve dependencies for {packages_list}')
        self._selected_packages = self._image.selected_packages = packages_list
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
            debian_status_file = os.path.join(item_path, 'var/lib/dpkg/status')
            alpine_installed_file = os.path.join(item_path, 'lib/apk/db/installed')

            if os.path.exists(debian_status_file):
                file_path = debian_status_file
                index_file_cls = DebianIndexFile
            elif os.path.exists(alpine_installed_file):
                file_path = alpine_installed_file
                index_file_cls = AlpineIndexFile
            else:
                continue

            distro, suite, arch = item_name.split('-')
            with index_file_cls(distro, suite, arch, file_path) as index_file:
                RPCHandler.base_packages_list[item_name] = []
                for package in index_file.iter_paragraphs():
                    RPCHandler.base_packages_list[item_name].append(package['package'])

            passwd_file = os.path.join(item_path, 'etc/passwd')
            with open(passwd_file, encoding='utf-8') as infile:
                RPCHandler.users_list[item_name] = []
                for line in infile:
                    RPCHandler.users_list[item_name].append(line.split(':'))

    LOGGER.info('RPC server is ready!')

    IOLoop().start(Application(), options.port)


if __name__ == "__main__":
    main()
