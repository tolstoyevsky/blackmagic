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

from shirow.server import RPCServer, remote

define('base_system',
       default='/var/blackmagic/jessie-armhf',
       help='The path to a chroot environment which contains '
            'the Debian base system')
define('packages_file',
       default='/var/blackmagic/Packages',
       help='The path to the Packages file')
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
    packages_list = []
    packages_number = 0
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.apt_lock = False
        self.global_lock = False
        self.lock_message = 'Locked'

    @remote
    def init(self, target_device):
        return 'Ready'

    @only_if_unlocked
    @remote
    def get_base_packages_list(self):
        return self.base_packages_list

    @only_if_unlocked
    @remote
    def get_dependencies_for(self, package_name):
        for package in self.packages_list:
            if package['package'] == package_name:
                dependencies_list = package['dependencies'] + ' '
                return re.findall('([-\w\d\.]+)[ ,]', dependencies_list)
        return []

    @only_if_unlocked
    @remote
    def get_packages_list(self, page_number, amount):
        start_position = (page_number - 1) * amount
        return self.packages_list[start_position:start_position + amount]

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

    if not os.path.isfile(options.packages_file):
        LOGGER.error('The Packages file specified via the packages_file '
                     'parameter does not exist')
        exit(1)

    if not os.path.isdir(options.workspace):
        LOGGER.error('The directory specified via the workspace parameter '
                     'does not exist')
        exit(1)

    app = Application()
    app.listen(options.port)

    passwd_file = os.path.join(options.base_system, 'etc/passwd')
    status_file = os.path.join(options.base_system, 'var/lib/dpkg/status')

    with open(options.packages_file, encoding='utf-8') as f:
        for package in deb822.Sources.iter_paragraphs(f):
            description_lines = package['description']

            RPCHandler.packages_list.append({
                'package': package['package'],
                'dependencies': package.get('depends', ''),
                'description': description_lines,
                'version': package['version'],
                'size': package['size'],
                'type': ''
            })

    RPCHandler.packages_number = len(RPCHandler.packages_list)

    with open(status_file, encoding='utf-8') as f:
        for package in deb822.Sources.iter_paragraphs(f):
            RPCHandler.base_packages_list.append(package['package'])

    with open(passwd_file, encoding='utf-8') as f:
        for line in f:
            RPCHandler.users_list.append(line.split(':'))

    LOGGER.info('RPC server is ready!')
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()
