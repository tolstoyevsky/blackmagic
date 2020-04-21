from functools import wraps

NOT_INITIALIZED = 11


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
