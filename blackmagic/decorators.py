from functools import wraps

from blackmagic.codes import NOT_INITIALIZED


def only_if_initialized(func):
    """Executes a remote procedure only if the RPC server is initialized. Every
    single remote procedure has to be decorated with only_if_initialized. The
    exception is 'init'.
    """

    @wraps(func)
    def wrapper(self, request, *args, **kwargs):
        if not self._global_lock:
            return func(self, request, *args, **kwargs)
        else:
            request.ret(NOT_INITIALIZED)

    return wrapper
