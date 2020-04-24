"""Module initializing the package."""

import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'blackmagic.docker')

django.setup()
