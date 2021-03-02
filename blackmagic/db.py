# Copyright 2020 Evgeny Golyshev. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid

from channels.db import database_sync_to_async
from django.contrib.auth.models import User

from blackmagic import defaults
from images.models import Image as ImageModel


class Image:
    """Class representing an image. """

    def __init__(self, user_id, device_name, distro_name, flavour):
        self._user_id = user_id
        self._device_name = device_name
        self._distro_name = distro_name
        self._flavour = flavour
        self._status = ImageModel.UNDEFINED
        self._selected_packages = []
        self._configuration = {}

        self.image_id = str(uuid.uuid4())

    def _get_configuration_props(self):
        props = {}
        for prop_key, prop_value in self._configuration.items():
            if (not self._configuration['enable_wireless']
                    and prop_key in defaults.WIRELESS_CONFIGURATION_KEYS):
                continue

            prop_name = f'PIEMAN_{prop_key.upper()}'
            props[prop_name] = prop_value

        return props

    @staticmethod
    def _serialize_props(props):
        for prop_key, prop_value in props.items():
            if isinstance(prop_value, bool):
                props[prop_key] = str(prop_value).lower()

    def enqueue(self):
        """Changes the image status to PENDING. """

        self._status = ImageModel.PENDING

    def set_selected_packages(self, selected_packages):
        self._selected_packages = selected_packages

    def set_configuration(self, configuration):
        self._configuration = configuration

    def dump_sync(self):
        try:
            image = ImageModel.objects.get(image_id=self.image_id)
        except ImageModel.DoesNotExist:
            image = ImageModel()

        image.user = User.objects.get(pk=self._user_id)
        image.image_id = self.image_id
        image.device_name = self._device_name
        image.distro_name = self._distro_name
        image.flavour = 'C'
        image.status = self._status

        configuration_props = self._get_configuration_props()
        props = {
            'PIEMAN_INCLUDES': ','.join(self._selected_packages),
            **configuration_props,
        }
        self._serialize_props(props)
        image.props = props

        image.save()

    @database_sync_to_async
    def dump(self):
        self.dump_sync()
