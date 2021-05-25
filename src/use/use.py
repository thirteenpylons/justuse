"""
A self-documenting, functional way to import modules in Python.

Goals:
- version check on the spot, potential version conflicts become obvious (done)
- load standalone-modules from online sources with hash-check (done)
- auto-reload on a given interval (done)
- auto-reload on file-change (done)
- pass module-level globals into the importing context (done)
- easy introspection of internal dependency graph (TODO)
- relative imports on online-sources (TODO)
- aspect-oriented decorators for anything on import (TODO) 

Examples:
>>> import use

# equivalent to `import numpy as np` with explicit version check
>>> np = use("numpy", version="1.1.1")
>>> np.version == "1.1.1"
True

# equivalent to `from pprint import pprint; pprint(dictionary)` but without assigning 
# pprint to a global variable, thus no namespace pollution
>>> use("pprint").pprint([1,2,3])
[1,2,3]
# equivalent to sys.path manipulation, then `import tools` with a reload(tools) every second
>>> tools = use("/media/sf_Dropbox/code/tools.py", reloading=True)

# it is possible to import standalone modules from online sources (which only import stdlib or use() other online-sources)
# with immediate sha1-hash-verificiation before execution of the code
>>> test = use(URL("https://raw.githubusercontent.com/PIA-Group/BioSPPy/7696d682dc3aafc898cd9161f946ea87db4fed7f/biosppy/utils.py"),
                    hash_value="77fe711647cd80e6a01668dba7f2b9feb8f435ba")

File-Hashing inspired by 
- https://github.com/kalafut/py-imohash
- https://github.com/fmoo/python-varint/blob/master/varint.py

:author: github@anselm.kiefner.de (Anselm Kiefner)
:license: MIT
"""

import asyncio
import hashlib
import imp
import importlib
import inspect
import os
import sys
import traceback
from functools import singledispatch, update_wrapper
from pathlib import Path
from types import ModuleType
from warnings import warn

import mmh3
import requests
from packaging.version import parse
from yarl import URL

__version__ = "0.1.0"

def methdispatch(func):
    dispatcher = singledispatch(func)
    def wrapper(*args, **kw):
        return dispatcher.dispatch(args[1].__class__)(*args, **kw)
    wrapper.register = dispatcher.register
    update_wrapper(wrapper, func)
    return wrapper


class VersionWarning(Warning):
    pass

class NotReloadableWarning(Warning):
    pass

class ModuleNotFoundError(RuntimeWarning):
    pass

class NoValidationWarning(Warning):
    pass

class UnexpectedHash(RuntimeWarning):
    pass


def varint_encode(number):
    """Pack `number` into varint bytes"""
    buf = b''
    while True:
        towrite = number & 0x7f
        number >>= 7
        if number:
            buf += bytes((towrite | 0x80,))
        else:
            buf += bytes((towrite,))
            break
    return buf

def hashfileobject(file, sample_threshhold=128 * 1024, sample_size=16 * 1024):
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0, os.SEEK_SET)
    if size < sample_threshhold or sample_size < 1:
        data = file.read()
    else:
        data = file.read(sample_size)
        f.seek(size//2)
        data += file.read(sample_size)
        f.seek(-sample_size, os.SEEK_END)
        data += file.read(sample_size)

    hash_tmp = mmh3.hash_bytes(data)
    hash_ = hash_tmp[7::-1] + hash_tmp[16:7:-1]
    enc_size = varint_encode(size)
    return enc_size + hash_[len(enc_size):]


def methdispatch(func):
    dispatcher = singledispatch(func)
    def wrapper(*args, **kw):
        return dispatcher.dispatch(args[1].__class__)(*args, **kw)
    wrapper.register = dispatcher.register
    update_wrapper(wrapper, func)
    return wrapper

def build_mod(name, code, initial_globals):
    mod = imp.new_module(name)
    mod.__dict__.update(initial_globals or {})
    exec(compile(code, name, "exec"), mod.__dict__)
    return mod

class SurrogateModule(ModuleType):
    def __init__(self, spec, initial_globals):
        self.__implementation = ModuleType("")
        
        async def __reload():
            last_filehash = None
            while True:
                with open(spec.origin, "rb") as file:
                    current_filehash = hashfileobject(file)
                    if current_filehash != last_filehash:
                        try:
                            file.seek(0)
                            mod = build_mod(spec.name, file.read(), initial_globals)
                            # TODO: check for different AST or globals
                            self.__implementation = mod
                        except Exception as e:
                            print(e, traceback.format_exc())
                    last_filehash = current_filehash
                await asyncio.sleep(1)
        self.__reloading = asyncio.get_event_loop().create_task(__reload())

    def __del__(self):
        self.__reloading.cancel()

    def __getattribute__(self, name):
        if name in ("_SurrogateModule__reloading",
                     "_SurrogateModule__implementation",
                    ):
            return object.__getattribute__(self, name)
        else:
            return getattr(self.__implementation, name)
    
    def __setattr__(self, name, value):
        if name in ("_SurrogateModule__reloading",
                     "_SurrogateModule__implementation",
                    ):
            object.__setattr__(self, name, value)
        else:
            setattr(self.__implementation, name, value)

class Use:
    __doc__ = __doc__  # module's __doc__ above

    def __init__(self):
        self.__using = {}

    @methdispatch
    def __call__(self, thing, *args, **kwargs):
        raise NotImplementedError(f"Only pathlib.Path, yarl.URL and str are valid sources of things to import, but got {type(thing)}.")

    @__call__.register(URL)
    def _use_url(self, url, hash_algo:str="sha1", hash_value:str=None, initial_globals:dict=None):
        response = requests.get(url)
        if response.status_code != 200:
            raise ModuleNotFoundError(f"Could not load {url} from the interwebs, got a {response.status_code} error.")
        if hash_value:
            if hash_algo != "sha1":
                raise NotImplementedError("At this moment only SHA1 is available.")
            this_hash = hashlib.sha1(response.content).hexdigest()
            if this_hash != hash_value:
                raise UnexpectedHash(f"{this_hash} does not match the expected hash {hash_value} - aborting!")
        else:
            warn(f"Attempting to import {url} from the interwebs with no validation whatsoever! This means we are flying blind and are possibly prone to man-in-the-middle attacks.")
        name = url.name
        mod = build_mod(name, response.content, initial_globals)
        self.__using[name] = mod, inspect.getframeinfo(inspect.currentframe())
        return mod

    @__call__.register(Path)
    def _use_path(self, path, reloading:bool=False, initial_globals:dict=None):
        path_set = set(sys.path)
        if path.is_dir():
            raise RuntimeWarning(f"Can't import directory {path}")
        elif path.is_file():
            path_set.add(str(path.resolve(strict=True).parents[0]))
            sys.path = list(path_set)
            name = path.stem
            # calling use() again
            if name in sys.modules:
                sys.modules[name].__reloading.cancel()
                del sys.modules[name]
            spec = importlib.machinery.PathFinder.find_spec(name)

            if reloading:
                mod = SurrogateModule(spec, initial_globals)
                if not getattr(mod, "__reloadable__", False):
                    warn(
                        f"Beware {name} is not flagged as reloadable, things may easily break!",
                        NotReloadableWarning,
                    )
            else:
                with open(path, "rb") as file:
                    mod = build_mod(name, file.read(), initial_globals)
            self.__using[name] = mod, inspect.getframeinfo(inspect.currentframe())
            return mod
        else:
            raise ModuleNotFoundError(f"Are you sure '{path}' exists?")

    @__call__.register(str)
    def _use_str(self, name, version:str=None, reloading:bool=False, initial_globals:dict=None):
        spec = importlib.machinery.PathFinder.find_spec(name)
        # builtins may have no spec, let's not mess with those
        # also, whole packages like numpy can't be reloaded
        if not spec or spec.parent:
            mod = importlib.import_module(name)
        else:
            if reloading:
                mod = SurrogateModule(spec, initial_globals)
                if not getattr(mod, "__reloadable__", False):
                    warn(
                        f"Beware {name} is not flagged as reloadable, things may easily break!",
                        NotReloadableWarning)
            else:
                mod = build_mod(name, spec.loader.get_source(name), initial_globals)

        self.__using[name] = name, mod, spec, inspect.getframeinfo(inspect.currentframe())

        if version:
            try:
                if parse(str(version)) != parse(str(mod.__version__)):
                    warn(
                        f"{name} is expected to be version {version} ,  but got {mod.__version__} instead",
                        VersionWarning,
                    )
            except AttributeError:
                print(f"Cannot determine version for module {name}, continueing.")
        return mod

sys.modules["use"] = Use()
