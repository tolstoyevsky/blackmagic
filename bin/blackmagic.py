#!/usr/bin/env python3
import logging
import os
import os.path
import shutil
import subprocess
import tarfile
import uuid
from functools import wraps

import configurations.management
# In spite of the fact that the above-mentioned import is never used throughout
# the code, the django.core.exceptions.ImproperlyConfigured exception will be
# raised if it's removed.
import django
import tornado.ioloop
import tornado.web
import tornado.websocket
from debian import deb822
from pymongo import MongoClient
from tornado.options import define, options

from firmwares.models import Firmware
from shirow.server import RPCServer, remote
from users.models import User

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
            # TODO: come up with a better solution than returning some message
            return self.lock_message

    return wrapper


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/([_\-\w\.]+)', RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = []
    packages_list = []  # TODO: get rid of
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.apt_proc = None
        self.user = None

        self.build_lock = False
        self.copy_lock = False
        self.global_lock = True
        self.lock_message = 'Locked'

        self.firmware_name = str(uuid.uuid4())
        self.rootfs = os.path.join(options.workspace, self.firmware_name)

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

    def destroy(self):
        if os.path.isdir(self.rootfs):
            LOGGER.debug('Remove {}'.format(self.rootfs))
            shutil.rmtree(self.rootfs)

    @remote
    def init(self, target_device):
        if not self.copy_lock:
            self.copy_lock = True
            LOGGER.debug('Start Copying {} to {}'.format(options.workspace,
                                                         self.rootfs))
            dst = os.path.join(options.workspace, self.rootfs)
            # TODO: use shutil.copytree
            command_line = ['cp', '-r', options.base_system, dst]
            proc = subprocess.Popen(command_line)
            proc.wait()
            LOGGER.debug('Finish copying')

            self.copy_lock = False
            self.global_lock = False

            return 'Ready'

    @only_if_unlocked
    @remote
    def build(self, packages_list):
        # TODO: get rid of the packages_list parameter because it's redundant

        if not self.build_lock:
            os.chdir(options.workspace)
            with tarfile.open(self.rootfs + '.tar.gz', 'w:gz') as tar:
                tar.add(self.firmware_name)

            firmware = Firmware(name=self.firmware_name, user=self._get_user())
            firmware.save()

            return 'Ready'

    @only_if_unlocked
    @remote
    def get_base_packages_list(self):
        return self.base_packages_list

    @remote
    def get_built_images(self):
        user = User.objects.get(id=self.user_id)
        firmwares = Firmware.objects.filter(user=user)
        return [firmware.name for firmware in firmwares]

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
        packages_list = []
        if query:
            matches = self.db.command('text', options.collection_name,
                                      search=query)
            if matches['results']:
                for document in matches['results']:
                    document['obj'].pop('_id')
                    packages_list.append(document['obj'])

        return packages_list

    @only_if_unlocked
    @remote
    def resolve(self, packages_list):
        if self.apt_proc:
            LOGGER.debug('APT process has been terminated')
            self.apt_proc.kill()

        packages_to_be_installed = set()

        command_line = ['chroot', options.base_system, '/usr/bin/apt-get',
                        'install', '--no-act', '-qq'] + packages_list
        self.apt_proc = subprocess.Popen(command_line,
                                         stdout=subprocess.PIPE,
                                         stdin=subprocess.PIPE)
        stdout_data, stderr_data = self.apt_proc.communicate()
        if self.apt_proc.returncode == -9:  # terminated
            return []

        for line in stdout_data.decode().splitlines():
            # The output of the above command line will look like the
            # following set of lines:
            # Inst libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
            # Inst libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
            # Inst libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
            # ...
            # Conf libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
            # Conf libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
            # Conf libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
            # ...
            #
            # The second word in each line is a package name.
            packages_to_be_installed.add(line.split(' ')[1])

        dependencies = packages_to_be_installed - set(packages_list)

        self.apt_proc = None

        return list(dependencies)  # Python sets are not JSON serializable


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

    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()
