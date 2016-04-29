from configurations import Configuration


class Base(Configuration):
    SECRET_KEY = 'gl3q^2f^fh)b=&g)*cah9h5n-d#if9k3s1#tnz2hre$1ea1zd^'


class Dev(Base):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql_psycopg2',
            'NAME': 'cusdeb',
            'USER': 'cusdeb',
            'PASSWORD': 'cusdeb',
            'HOST': 'localhost',
            'PORT': '',
        }
    }


class Prod(Base):
    pass  # TODO: specify configuration for the Prod environment


class Test(Dev):
    PAUSE = 10  # sec
