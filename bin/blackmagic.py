#!/usr/bin/env python3
import logging
import os
import os.path
import re
import subprocess
from functools import wraps

import tornado.ioloop
import tornado.web
import tornado.websocket
from debian import deb822

from tornado.options import define, options
from pymongo import MongoClient
from shirow.server import RPCServer, remote

define('base_system',
       default='/var/blackmagic/jessie-armhf',
       help='The path to a chroot environment which contains '
            'the Debian base system')
define('collection_name',
       default='jessie-armhf',
       help='')
define('db_name',
       default='cusdeb',
       help='')
define('mongodb_host',
       default='localhost',
       help='')
define('mongodb_port',
       default=27017,
       help='')
define('workspace',
       default='/var/blackmagic/workspace')

LOGGER = logging.getLogger('tornado.application')


def only_if_unlocked(func):
    """Executes a remote procedure only if the RPC server is unlocked. Every
    single remote procedure, except initialize, has to be decorated with
    only_if_unlocked."""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.global_lock:
            return func(self, *args, **kwargs)
        else:
            return self.lock_message

    return wrapper


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/([\w\.]+)', RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = []
    packages_list = []  # TODO: get rid of
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.apt_lock = False
        self.global_lock = False
        self.lock_message = 'Locked'

        client = MongoClient(options.mongodb_host, options.mongodb_port)
        self.db = client[options.db_name]
        self.collection = self.db[options.collection_name]

        self.packages_number = self.collection.find().count()

    @remote
    def init(self, target_device):
        return 'Ready'

    @only_if_unlocked
    @remote
    def build(self, packages_list):
        return 'Ready'

    @only_if_unlocked
    @remote
    def get_base_packages_list(self):
        return self.base_packages_list

    @only_if_unlocked
    @remote
    def get_built_images(self):
        return []

    @only_if_unlocked
    @remote
    def get_dependencies_for(self, package_name):
        document = self.collection.find_one({'package': package_name})
        if document:
            dependencies_list = document['dependencies'] + ' '
            return re.findall('([-\w\d\.]+)[ ,]', dependencies_list)
        return []

    @only_if_unlocked
    @remote
    def get_packages_list(self, page_number, per_page):
        if page_number > 0:
            start_position = (page_number - 1) * per_page
        else:
            start_position = 0

        collection = self.collection
        packages_list = []
        for document in collection.find().skip(start_position).limit(per_page):
            # Originally _id is an ObjectId instance and it's not JSON
            # serializable
            document['_id'] = str(document['_id'])
            packages_list.append(document)

        return packages_list

    @only_if_unlocked
    @remote
    def get_target_devices_list(self):
        target_devices_list = [
            'Raspberry Pi 2',
        ]
        return target_devices_list

    @only_if_unlocked
    @remote
    def get_packages_number(self):
        return self.packages_number

    @only_if_unlocked
    @remote
    def get_users_list(self):
        return self.users_list

    @only_if_unlocked
    @remote
    def search(self, query):
        matches = self.db.command('text', options.collection_name,
                                  search=query)
        packages_list = []
        if matches['results']:
            for document in matches['results']:
                document['obj'].pop('_id')
                packages_list.append(document['obj'])

        return packages_list

    @only_if_unlocked
    @remote
    def resolve(self, packages_list):
        if not self.apt_lock:
            self.apt_lock = True

            command = ['chroot', options.base_system, '/usr/bin/apt-get',
                       'install', '--no-act', '-qq'] + packages_list
            proc = subprocess.Popen(command,
                                    stdout=subprocess.PIPE,
                                    stdin=subprocess.PIPE)
            stdout_data, stderr_data = proc.communicate()
            res = re.findall('Inst ([-\w\d\.]+)', str(stdout_data))

            self.apt_lock = False

            return list(set(res) - set(packages_list))
        return self.lock_message


def main():
    tornado.options.parse_command_line()
    if os.getuid() > 0:
        LOGGER.error('The server can only be run by root')
        exit(1)

    if not os.path.isdir(options.base_system):
        LOGGER.error('The directory specified via the base_system parameter '
                     'does not exist')
        exit(1)

    if not os.path.isdir(options.workspace):
        LOGGER.error('The directory specified via the workspace parameter '
                     'does not exist')
        exit(1)

    app = Application()
    app.listen(options.port)

    passwd_file = os.path.join(options.base_system, 'etc/passwd')
    status_file = os.path.join(options.base_system, 'var/lib/dpkg/status')

    with open(passwd_file, encoding='utf-8') as f:
        for line in f:
            RPCHandler.users_list.append(line.split(':'))

    with open(status_file, encoding='utf-8') as f:
        for package in deb822.Packages.iter_paragraphs(f):
            RPCHandler.base_packages_list.append(package['package'])

    LOGGER.info('RPC server is ready!')

    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()
