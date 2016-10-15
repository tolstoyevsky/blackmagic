#!/usr/bin/env python3
import logging
import os
import os.path
import re
import shutil
import subprocess
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
BUSY = 12
PREPARE_ENV = 14
MARK_ESSENTIAL_PACKAGES_AS_INSTALLED = 15
INSTALL_KEYRING_PACKAGE = 16
UPDATE_INDICES = 17


def only_if_unlocked(func):
    """Executes a remote procedure only if the RPC server is unlocked. Every
    single remote procedure has to be decorated with only_if_unlocked. The
    exceptions are:
    * init
    * get_built_images
    * get_target_devices_list"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.global_lock:
            return func(self, *args, **kwargs)
        else:
            return self._say_server_locked()

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

        self.build_id = None
        self.resolver_env = ''

        self.inst_pattern = re.compile('Inst ([-\.\w]+)')

        self.user = None

        self.root_password = DEFAULT_ROOT_PASSWORD
        self.users = []

        self.build_lock = False
        self.global_lock = True
        self.init_lock = False
        self.lock_message = 'Locked'

        self.selected_packages = []

        self.target = {}

        client = MongoClient(options.mongodb_host, options.mongodb_port)
        self.db = client[options.db_name]
        self.collection = self.db[options.collection_name]

        self.packages_number = self.collection.find().count()

    def _get_user(self):
        if not self.user:
            self.user = User.objects.get(id=self.user_id)
            return self.user
        else:
            return self.user

    def _remove_resolver_env(self):
        if os.path.isdir(self.resolver_env):
            LOGGER.debug('Remove {}'.format(self.resolver_env))
            shutil.rmtree(self.resolver_env)

    @gen.coroutine
    def _say_server_locked(self):
        return self.lock_message

    def destroy(self):
        self._remove_resolver_env()

    @remote
    def init(self, request, name, target_device, distro, distro_suite):
        if self.init_lock:
            request.ret(self.lock_message)

        if self.build_lock:
            request.ret(BUSY)

        self.init_lock = True

        if self.resolver_env:
            self._remove_resolver_env()

        self.build_id = str(uuid.uuid4())
        self.resolver_env = os.path.join(options.workspace, self.build_id)

        self.target = {
            'distro': '{} {}'.format(distro, distro_suite),
            'device': target_device
        }

        LOGGER.debug('Creating hierarchy in {}'.format(self.resolver_env))
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
            os.makedirs(self.resolver_env + directory)

        request.ret_and_continue(MARK_ESSENTIAL_PACKAGES_AS_INSTALLED)
        yield gen.sleep(1)

        shutil.copyfile(options.status_file,
                        self.resolver_env + '/var/lib/dpkg/status')

        with open(self.resolver_env + '/etc/apt/sources.list', 'w') as f:
            f.write('deb http://ftp.ru.debian.org/debian jessie main')

        request.ret_and_continue(INSTALL_KEYRING_PACKAGE)
        yield gen.sleep(1)

        command_line = [
            'dpkg', '-x', options.keyring_package, self.resolver_env
        ]
        proc = Subprocess(command_line)
        yield proc.wait_for_exit()

        LOGGER.debug('Executing apt-get update')
        request.ret_and_continue(UPDATE_INDICES)

        command_line = [
            'apt-get', 'update', '-qq',
            '-o', 'APT::Architecture=all',
            '-o', 'APT::Architecture=armhf',
            '-o', 'Dir=' + self.resolver_env,
            '-o', 'Dir::State::status=' + self.resolver_env +
                  '/var/lib/dpkg/status'
        ]
        proc = Subprocess(command_line)
        yield proc.wait_for_exit()

        LOGGER.debug('Finishing initialization')

        self.init_lock = False
        self.global_lock = False

        request.ret(READY)

    @only_if_unlocked
    @remote
    def build(self, request):
        if not self.build_lock:
            self.build_lock = True

            result = AsyncResult(build.delay(self.user_id, self.build_id,
                                             self.selected_packages, self.root_password,
                                             self.users, self.target))
            while not result.ready():
                yield gen.sleep(1)

            self.build_lock = False

            self._remove_resolver_env()

            request.ret(READY)

        request.ret(self.lock_message)

    @only_if_unlocked
    @remote
    def add_user(self, request, username, password, uid, gid, comment, homedir, shell):
        self.users.append({
            'username': username,
            'password': password,
            'uid': uid,
            'gid': gid,
            'comment': comment,
            'homedir': homedir,
            'shell': shell
        })
        request.ret('ok')

    @only_if_unlocked
    @remote
    def change_root_password(self, request, password):
        self.root_password = password
        LOGGER.debug(self.root_password)
        request.ret(READY)

    @only_if_unlocked
    @remote
    def get_base_packages_list(self, request):
        request.ret(self.base_packages_list)

    @remote
    def get_built_images(self, request):
        user = User.objects.get(id=self.user_id)
        firmwares = Firmware.objects.filter(user=user)
        request.ret([firmware.name for firmware in firmwares])

    @only_if_unlocked
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

    @only_if_unlocked
    @remote
    def get_default_root_password(self, request):
        request.ret(DEFAULT_ROOT_PASSWORD)

    @only_if_unlocked
    @remote
    def get_shells_list(self, request):
        request.ret(['/bin/sh', '/bin/dash', '/bin/bash', '/bin/rbash'])

    @remote
    def get_target_devices_list(self, request):
        target_devices_list = [
            'Raspberry Pi 2',
        ]
        request.ret(target_devices_list)

    @only_if_unlocked
    @remote
    def get_packages_number(self, request):
        request.ret(self.packages_number)

    @only_if_unlocked
    @remote
    def get_users_list(self, request):
        request.ret(self.users_list)

    @only_if_unlocked
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

    @only_if_unlocked
    @remote
    def resolve(self, request, packages_list):
        self.selected_packages = packages_list

        command_line = [
            'apt-get', 'install', '--no-act', '-qq',
            '-o', 'APT::Architecture=all',
            '-o', 'APT::Architecture=armhf',
            '-o', 'Dir=' + self.resolver_env,
            '-o', 'Dir::State::status=' + self.resolver_env +
                  '/var/lib/dpkg/status'
        ] + packages_list

        apt_proc = subprocess.Popen(command_line,
                                    stdout=subprocess.PIPE,
                                    stdin=subprocess.PIPE)
        stdout_data, stderr_data = apt_proc.communicate()

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
        packages_to_be_installed = self.inst_pattern.findall(str(stdout_data))
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
