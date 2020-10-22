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

from images import models


class Image:
    """Class representing an image. """

    def __init__(self, user_id, name, device_name, distro_name, flavour):
        self._user_id = user_id
        self._name = name
        self._device_name = device_name
        self._distro_name = distro_name
        self._flavour = flavour
        self._status = models.Image.UNDEFINED

        self.image_id = str(uuid.uuid4())

    def enqueue(self):
        """Changes the image status to PENDING. """

        self._status = models.Image.PENDING

    def dump_sync(self):
        image = models.Image()

        image.user = User.objects.get(pk=self._user_id)
        image.image_id = self.image_id
        image.device_name = 'rpi-zero-w'
        image.distro_name = self._distro_name
        image.flavour = 'C'
        image.status = self._status

        image.save()

    @database_sync_to_async
    def dump(self):
        self.dump_sync()
