#!/usr/bin/env python3
import logging
import os
import os.path
import re
import shutil
import uuid
from functools import wraps

import django
import tornado.web
import tornado.options
from celery.result import AsyncResult
from debian import deb822
from django.conf import settings
from dominion.tasks import build
from pymongo import MongoClient
from shirow import util
from tornado import gen
from tornado.options import define, options
from tornado.process import Subprocess

from firmwares.models import Firmware
from shirow.ioloop import IOLoop
from shirow.server import RPCServer, TOKEN_PATTEN, remote
from users.models import User

define('base_system',
       default='/var/blackmagic/jessie-armhf',
       help='The path to a chroot environment which contains '
            'the Debian base system')
define('collection_name',
       default='jessie-armhf',
       help='')
define('db_name',
       default=settings.MONGO['DATABASE'],
       help='')
define('keyring_package',
       default='/var/blackmagic/debian-archive-keyring_2014.3_all.deb',
       help='')
define('mongodb_host',
       default=settings.MONGO['HOST'],
       help='')
define('mongodb_port',
       default=settings.MONGO['PORT'],
       help='')
define('status_file',
       default='/var/blackmagic/status',
       help='')
define('workspace',
       default='/var/blackmagic/workspace')

LOGGER = logging.getLogger('tornado.application')

DEFAULT_ROOT_PASSWORD = 'cusdeb'

READY = 10
NOT_INITIALIZED = 11
BUSY = 12
LOCKED = 13
PREPARE_ENV = 14
MARK_ESSENTIAL_PACKAGES_AS_INSTALLED = 15
INSTALL_KEYRING_PACKAGE = 16
UPDATE_INDICES = 17
BUILD_FAILED = 18


def only_if_initialized(func):
    """Executes a remote procedure only if the RPC server is initialized. Every
    single remote procedure has to be decorated with only_if_initialized. The
    exceptions are:
    * init
    * get_built_images
    * get_target_devices_list"""

    @wraps(func)
    def wrapper(self, request, *args, **kwargs):
        if not self.global_lock:
            return func(self, request, *args, **kwargs)
        else:
            request.ret(NOT_INITIALIZED)

    return wrapper


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/' + TOKEN_PATTEN, RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = []
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.build_lock = False
        self.global_lock = True
        self.init_lock = False

        self.image = {
            'id': None,
            'resolver_env': '',
            'root_password': DEFAULT_ROOT_PASSWORD,
            'selected_packages': [],
            'target': {},
            'users': [],
            'configuration': [],
        }

        self.inst_pattern = re.compile('Inst ([-\.\w]+)')

        self.user = None  # the one who builds an image

        client = MongoClient(options.mongodb_host, options.mongodb_port)
        self.db = client[options.db_name]
        self.collection = self.db[options.collection_name]

        self.packages_number = self.collection.find().count()
        self.enable_email_notifications = False

    def _get_user(self):
        if not self.user:
            self.user = User.objects.get(id=self.user_id)
            return self.user
        else:
            return self.user

    def _remove_resolver_env(self):
        if os.path.isdir(self.image['resolver_env']):
            LOGGER.debug('Remove {}'.format(self.image['resolver_env']))
            shutil.rmtree(self.image['resolver_env'])

    def destroy(self):
        self._remove_resolver_env()

    @remote
    def init(self, request, name, target_device, distro):
        if self.init_lock:
            request.ret(LOCKED)

        if self.build_lock:
            request.ret(BUSY)

        self.init_lock = True

        if self.image['resolver_env']:
            self._remove_resolver_env()

        self.image['id'] = build_id = str(uuid.uuid4())
        self.image['resolver_env'] = resolver_env = \
            os.path.join(options.workspace, build_id)

        self.image['target'] = {
            'distro': distro,
            'device': target_device
        }

        LOGGER.debug('Creating hierarchy in {}'.format(resolver_env))
        request.ret_and_continue(PREPARE_ENV)
        # Such things like preparing resolver environment, marking essential
        # packages as installed and installing debian-archive-keyring package
        # don't take much time, so we let users know what's going on by adding
        # small pauses.
        yield gen.sleep(1)

        hiera = [
            '/etc/apt',
            '/etc/apt/preferences.d',
            '/var/cache/apt/archives/partial',
            '/var/lib/apt/lists/partial',
            '/var/lib/dpkg',
        ]
        for directory in hiera:
            os.makedirs(resolver_env + directory)

        request.ret_and_continue(MARK_ESSENTIAL_PACKAGES_AS_INSTALLED)
        yield gen.sleep(1)

        shutil.copyfile(options.status_file,
                        resolver_env + '/var/lib/dpkg/status')

        with open(resolver_env + '/etc/apt/sources.list', 'w') as f:
            f.write('deb http://ftp.ru.debian.org/debian jessie main')

        request.ret_and_continue(INSTALL_KEYRING_PACKAGE)
        yield gen.sleep(1)

        command_line = ['dpkg', '-x', options.keyring_package, resolver_env]
        output = yield util.run(command_line)
        LOGGER.debug('dpkg: {}'.format(output))

        LOGGER.debug('Executing apt-get update')
        request.ret_and_continue(UPDATE_INDICES)

        command_line = [
            'apt-get', 'update', '-qq',
            '-o', 'APT::Architecture=all',
            '-o', 'APT::Architecture=armhf',
            '-o', 'Dir=' + resolver_env,
            '-o', 'Dir::State::status=' + resolver_env +
                  '/var/lib/dpkg/status'
        ]
        proc = Subprocess(command_line)
        yield proc.wait_for_exit()

        LOGGER.debug('Finishing initialization')

        self.init_lock = False
        self.global_lock = False

        request.ret_and_continue(build_id)

        request.ret(READY)

    @only_if_initialized
    @remote
    def build(self, request):
        if not self.build_lock:
            self.build_lock = True

            result = AsyncResult(build.delay(self.user_id, self.image))
            while not result.ready():
                yield gen.sleep(1)

            self.build_lock = False

            self._remove_resolver_env()

            if result.get() == 0:
                request.ret(READY)
            else:
                request.ret(BUILD_FAILED)

        request.ret(LOCKED)

    @only_if_initialized
    @remote
    def add_user(self, request, username, password, uid, gid, comment, homedir,
                 shell):
        self.image['users'].append({
            'username': username,
            'password': password,
            'uid': uid,
            'gid': gid,
            'comment': comment,
            'homedir': homedir,
            'shell': shell
        })
        request.ret(READY)

    @only_if_initialized
    @remote
    def change_root_password(self, request, password):
        self.image['root_password'] = password
        request.ret(READY)

    @only_if_initialized
    @remote
    def sync_configuration(self, request, image_configuration_params):
        self.image['configuration'] = image_configuration_params
        request.ret(READY)

    @only_if_initialized
    @remote
    def enable_email_notifications(self, request, enable_email_notifications):
        self.enable_email_notifications = enable_email_notifications
        request.ret(READY)

    @only_if_initialized
    @remote
    def get_base_packages_list(self, request):
        request.ret(self.base_packages_list)

    @remote
    def get_built_images(self, request):
        user = User.objects.get(id=self.user_id)
        firmwares = Firmware.objects.filter(user=user)
        request.ret([firmware.name for firmware in firmwares])

    @only_if_initialized
    @remote
    def get_packages_list(self, request, page_number, per_page):
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

        request.ret(packages_list)

    @only_if_initialized
    @remote
    def get_default_root_password(self, request):
        request.ret(DEFAULT_ROOT_PASSWORD)

    @only_if_initialized
    @remote
    def get_shells_list(self, request):
        request.ret(['/bin/sh', '/bin/dash', '/bin/bash', '/bin/rbash'])

    @remote
    def get_target_devices_list(self, request):
        target_devices_list = [
            'Raspberry Pi 2',
            'Raspberry Pi 3',
        ]
        request.ret(target_devices_list)

    @only_if_initialized
    @remote
    def get_packages_number(self, request):
        request.ret(self.packages_number)

    @only_if_initialized
    @remote
    def get_users_list(self, request):
        request.ret(self.users_list)

    @only_if_initialized
    @remote
    def search(self, request, query):
        packages_list = []
        if query:
            matches = self.db.command('text', options.collection_name,
                                      search=query)
            if matches['results']:
                for document in matches['results']:
                    document['obj'].pop('_id')
                    packages_list.append(document['obj'])

        request.ret(packages_list)

    @only_if_initialized
    @remote
    def resolve(self, request, packages_list):
        self.image['selected_packages'] = packages_list
        resolver_env = self.image['resolver_env']

        command_line = [
            'apt-get', 'install', '--no-act', '-qq',
            '-o', 'APT::Architecture=all',
            '-o', 'APT::Architecture=armhf',
            '-o', 'Dir=' + resolver_env,
            '-o', 'Dir::State::status=' + resolver_env +
                  '/var/lib/dpkg/status'
        ] + packages_list

        data = yield util.run(command_line)

        # The output of the above command line will look like the
        # following set of lines:
        # NOTE: This is only a simulation!
        #       apt-get needs root privileges for real execution.
        #       Keep also in mind that locking is deactivated,
        #       so don't depend on the relevance to the real current situation!
        # Inst libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
        # Inst libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
        # Inst libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
        # ...
        # Conf libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
        # Conf libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
        # Conf libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
        # ...
        packages_to_be_installed = self.inst_pattern.findall(str(data))
        dependencies = set(packages_to_be_installed) - set(packages_list)

        # Python sets are not JSON serializable
        request.ret(list(dependencies))


def main():
    tornado.options.parse_command_line()
    if not os.path.isdir(options.base_system):
        LOGGER.error('The directory specified via the base_system parameter '
                     'does not exist')
        exit(1)

    if not os.path.isfile(options.keyring_package):
        LOGGER.error('The file specified via the keyring_package parameter '
                     'does not exist')
        exit(1)

    if not os.path.isfile(options.status_file):
        LOGGER.error('The file specified via the status_file parameter '
                     'does not exist')
        exit(1)

    if not os.path.isdir(options.workspace):
        LOGGER.error('The directory specified via the workspace parameter '
                     'does not exist')
        exit(1)

    django.setup()

    passwd_file = os.path.join(options.base_system, 'etc/passwd')
    status_file = os.path.join(options.base_system, 'var/lib/dpkg/status')

    with open(passwd_file, encoding='utf-8') as f:
        for line in f:
            RPCHandler.users_list.append(line.split(':'))

    with open(status_file, encoding='utf-8') as f:
        for package in deb822.Packages.iter_paragraphs(f):
            RPCHandler.base_packages_list.append(package['package'])

    LOGGER.info('RPC server is ready!')

    IOLoop().start(Application(), options.port)

if __name__ == "__main__":
    main()
