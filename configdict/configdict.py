from __future__ import annotations

import appdirs
import os
import json
import logging
import sys
import re
import weakref
import textwrap
from types import FunctionType
from typing import (Optional as Opt, Any, Tuple, Dict)

__all__ = ["CheckedDict", "ConfigDict", "getConfig", "activeConfigs"]

logger = logging.getLogger("configdict")

_UNKNOWN = object()


def _checkValidator(validatordict: dict, defaultdict: dict) -> dict:
    """
    Checks the validity of the validator itself, and makes any needed
    postprocessing on the validator

    Args:
        validatordict: the validator dict
        defaultdict: the dict containing defaults

    Returns:
        a postprocessed validator dict
    """
    stripped_keys = {key.split("::")[0] for key in validatordict.keys()}
    not_present = stripped_keys-defaultdict.keys()
    if any(not_present):
        notpres = ", ".join(sorted(not_present))
        raise KeyError(
                f"The validator dict has keys not present in the defaultdict ({notpres})"
        )
    v = {}
    for key, value in validatordict.items():
        if key.endswith('::choices') and isinstance(value, (list, tuple)):
            value = set(value)
        v[key] = value
    return v


def _isfloaty(value):
    return isinstance(value, (int, float)) or hasattr(value, '__float__')


def _openInStandardApp(path:str) -> None:
    """
    Open path with the app defined to handle it by the user
    at the os level (xdg-open in linux, start in win, open in osx)
    """
    import subprocess
    platform = sys.platform
    if platform == 'linux':
        subprocess.call(["xdg-open", path])
    elif platform == "win32":
        os.startfile(path)
    elif platform == "darwin":
        subprocess.call(["open", path])
    else:
        raise RuntimeError(f"platform {platform} not supported")


def _wait_on_file_modified(path:str, timeout:float=None) -> bool:
    try:
        from watchdog.observers import Observer
        from watchdog.events import PatternMatchingEventHandler
    except ImportError:
        logger.info("watchdog is needed to be able to wait on file events. Install via `pip install watchdog`")   
        _waitOnClick()
        return
        
    import time
    directory, base = _os.path.split(path)
    if not directory:
        directory = "."
    handler = PatternMatchingEventHandler([base], ignore_patterns="",
                                          ignore_directories=True, case_sensitive=True)
    observer = Observer()
    modified = False

    def on_modified(event):
        nonlocal modified
        modified = True
        observer.stop()

    handler.on_modified = on_modified
    observer.schedule(handler, path=directory, recursive=False)
    observer.start()
    if timeout is None:
        timeout = 360000  # 100 hours
    observer.join(timeout)
    return modified


def _dialog_show_info(msg, title=None):
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    window.wm_withdraw()
    messagebox.showinfo(title, msg)
    window.destroy()


def _waitForClick():
    _dialog_show_info("Click OK when finished editing")


def _openInEditor(cfg):
    _openInStandardApp(cfg)


class CheckedDict(dict):
    def __init__(self,
                 default: Dict[str, Any] = None,
                 validator: Dict[str, Any] = None,
                 docs: Dict[str, str] = None,
                 callback=None,
                 precallback=None) -> None:
        """
        A dictionary which checks that the keys and values are valid
        according to a default dict and a validator.

        Args:
            default: a dict will all default values. A config can accept only
                keys which are already present in the default

            validator: a dict containing choices and types for the keys in the
                default. Given a default like: {'keyA': 'foo', 'keyB': 20},
                a validator could be:

                {'keyA::choices': ['foo', 'bar'],
                 'keyB::type': float,
                 'keyC::range': (0, 1)
                }

                choices can be defined lazyly by giving a lambda which returns a list
                of possible choices

            docs: a dict containing help lines for keys defined in default
            callback:
                function (key, value) -> None
                This function is called AFTER the modification has been done.
        """
        self.default = default if default else {}
        self._validator = _checkValidator(validator,
                                          default) if validator else {}
        self._docs = docs if docs else {}
        self._allowedkeys = set(default.keys()) if default else set()
        self._precallback = precallback
        self._callback = callback

    def _changed(self):
        self._allowedkeys = set(self.default.keys())

    def copy(self) -> CheckedDict:
        out = CheckedDict(default=self.default, validator=self._validator, docs=self._docs,
                          precallback=self._precallback, callback=self._callback)
        return out

    def diff(self) -> dict:
        """
        Get a dict containing keys:values which differ from default
        """
        out = {}
        default = self.default
        for key, value in self.items():
            valuedefault = default[key]
            if value != valuedefault:
                out[key] = value
        return out

    def addKey(self,
               key: str,
               value,
               type=None,
               choices=None,
               range: Tuple[Any, Any] = None,
               doc: str = None) -> None:
        """
        Add a key: value pair to the default settings. This is used when building the
        default config item by item (see example). After adding all new keys it is
        necessary to call .load()

        Example:
            cfg = ConfigDict("foo", load=False)
            # We define a default step by step
            cfg.addKey("size", 100, range=(50, 150))
            cfg.addKey("color", "red", choices=("read", "blue", "green"))
            # Now update the dict with the newly defined default and any
            # saved version
            cfg.load()

        Args:
            key: a string key
            value: a default value
            type: the type accepted, as passed to isinstance (can be a tuple)
            choices: a seq of possible values
            range: a (min, max) tuple defining allowed range
            doc: documentation for this key

        """
        self.default[key] = value
        self._allowedkeys.add(key)
        validator = self._validator
        if type:
            validator[f"{key}::type"] = type
        if choices:
            validator[f"{key}::choices"] = choices
        if range:
            validator[f"{key}::range"] = range
        if doc:
            self._docs[key] = doc

    def __setitem__(self, key: str, value) -> None:
        if key not in self._allowedkeys:
            raise KeyError(f"Unknown key: {key}")
        oldvalue = self.get(key)
        if oldvalue is not None and oldvalue == value:
            return
        errormsg = self.checkValue(key, value)
        if errormsg:
            raise ValueError(errormsg)
        if self._precallback:
            newvalue = self._precallback(self, key, oldvalue, value)
            if newvalue:
                value = newvalue

        super().__setitem__(key, value)

        if self._callback is not None:
            self._callback(key, value)

    def checkDict(self, d: dict) -> str:
        invalidkeys = [key for key in d if key not in self.default]
        if invalidkeys:
            return f"Some keys are not valid: {invalidkeys}"
        for k, v in d.items():
            errormsg = self.checkValue(k, v)
            if errormsg:
                return errormsg
        return ""

    def getChoices(self, key: str) -> Opt[list]:
        """
        Return a seq. of possible values for key `k`
        or None
        """
        if key not in self._allowedkeys:
            raise KeyError(f"{key} is not a valid key")
        if not self._validator:
            logger.debug("getChoices: validator not set")
            return None
        key2 = key+"::choices"
        choices = self._validator.get(key2, None)
        if isinstance(choices, FunctionType):
            realchoices = choices()
            self._validator[key2] = set(realchoices)
            return realchoices
        return choices

    def getDoc(self, key: str) -> Opt[str]:
        """ Get documentation for key (if present) """
        if self._docs:
            return self._docs.get(key)

    def checkValue(self, key: str, value) -> Opt[str]:
        """
        Check if value is valid for key

        Returns errormsg. If value is of correct type, errormsg is None

        Example:

        error = config.checkType(key, value)
        if error:
            print(error)
        """
        choices = self.getChoices(key)
        if choices is not None and value not in choices:
            return f"key {key} should be one of {choices}, got {value}"
        t = self.getType(key)
        if t == float:
            if not _isfloaty(value):
                return f"Expected floatlike for key {key}, got {type(value).__name__}"
        elif t == str and not isinstance(value, (bytes, str)):
            return f"Expected str or bytes for key {key}, got {type(value).__name__}"
        elif not isinstance(value, t):
            return f"Expected {t.__name__} for key {key}, got {type(value).__name__}"
        r = self.getRange(key)
        if r and not (r[0]<=value<=r[1]):
            return f"Value for key {key} should be within range {r}, got {value}"
        return None

    def getRange(self, key: str) -> Opt[tuple]:
        if key not in self._allowedkeys:
            raise KeyError(f"{key} is not a valid key")
        if not self._validator:
            logger.debug("getChoices: validator not set")
            return None
        return self._validator.get(key+"::range", None)

    def getType(self, key: str):
        """
        Returns the expected type for key, as a type

        NB: all numbers are reduced to type float, all strings are of type str,
            otherwise the type of the default value, which can be a collection
            like a list or a dict

        See Also: checkValue
        """
        if self._validator is not None:
            definedtype = self._validator.get(key+"::type")
            if definedtype:
                return definedtype
            choices = self.getChoices(key)
            if choices:
                types = set(type(choice) for choice in choices)
                if len(types) == 1:
                    return type(next(iter(choices)))
                return tuple(types)
        defaultval = self.default.get(key, _UNKNOWN)
        if defaultval is _UNKNOWN:
            raise KeyError(f"Key {key} is not present in default config. "
                           f"Possible keys: {list(self.default.keys())}")
        return str if isinstance(defaultval,
                                 (bytes, str)) else type(defaultval)

    def getTypestr(self, key: str) -> str:
        t = self.getType(key)
        if isinstance(t, tuple):
            return "("+", ".join(x.__name__ for x in t)+")"
        else:
            return t.__name__

    def reset(self) -> None:
        """
        Resets the config to its default (inplace), and saves it.

        Example
        ~~~~~~~

        cfg = getconfig("folder:config")
        cfg = cfg.reset()
        """
        self.clear()
        self.update(self.default)

    def update(self, d: dict=None, **kws) -> None:
        if d:
            errormsg = self.checkDict(d)
            if errormsg:
                raise ValueError(f"dict is invalid: {errormsg}")
            super().update(d)
        if kws:
            errormsg = self.checkDict(kws)
            if errormsg:
                raise ValueError(f"invalid keywords: {errormsg}")
            super().update(kws)

    def override(self, key: str, value, default=None):
        """
        The same as `value if value is not None else config.get(key, default)
        """
        return value if value is not None else self.get(key, default)


class ConfigDict(CheckedDict):
    registry: Dict[str, ConfigDict] = {}
    _helpwidth: int = 58

    def __init__(self,
                 name: str,
                 default: Dict[str, Any] = None,
                 validator: Dict[str, Any] = None,
                 docs: Dict[str, str] = None,
                 precallback=None,
                 persistent=True,
                 load=True) -> None:
        """
        This is a (optionally) persistent, unique dictionary used for configuration
        of a module / app. It is saved under the config folder determined by
        the OS (and is thus OS dependent) and no two instances of the same
        config can coexist.

        Args:
            name: a str of the form ``prefix:config`` or ``prefix/config`` or ``prefix.config``
                (these are the same) or simply ``config`` if this is an
                isolated configuration (not part of a bigger project). The
                json data will be saved at ``$USERCONFIGDIR/folder/{name}.json``
                For instance, in Linux for name mydir:myconfig this would be:
                ~/.config/mydir/myconfig.json

            default: a dict with all default values. A config can accept only
                keys which are already present in the default

            validator: a dict containing choices and types for the keys in the
                default. Given a default like: ``{'keyA': 'foo', 'keyB': 20}``,
                a validator could be:

                {
                  'keyA::choices': ['foo', 'bar'],
                  'keyB::type': float
                }

                Choices can be defined lazyly by giving a lambda

            precallback:
                function (self, key, oldvalue, newvalue) -> None|newvalue,
                If given, it is called BEFORE the modification is done
                * return None to allow modification
                * return any value to modify the value
                * raise a ValueError exception to stop the transaction

        Example:
            default = {
                "keyA": 10,
                "keyB": 0.5,
                "keyC": "blue
            }

            validator = {
                "keyB::range" = (0, 1),
                "keyC::choices" = ("blue", "red")
            }

            docs = {
                "keyA": "documentation of keyA"
                "keyC": "documentation of keyC"
            }

            cfg = ConfigDict("myproj:subproj",
                             default=default,
                             validator=validator,
                             docs=docs)

            A ConfigDict can also be defined item by item

            config = ConfigDict("myproj:subproj")
            config.addKey("keyA", 10, doc="documentaion of keyA")
            config.addKey("keyB", 0.5, range=(0, 1))
            config.addKey("keyC", "blue", choices=("blue", "red"),
                          doc="documentation of keyC")
            config.load()

        """
        if name:
            name = _normalizeName(name)
            if not _isValidName(name):
                raise ValueError(f"name {name} is invalid for a config")
        if name in ConfigDict.registry:
            logger.warning("A ConfigDict with the given name already exists!")
        cfg = getConfig(name)
        if cfg and default != cfg.default:
            logger.debug(f"ConfigDict: config with name {name} already created"
                         "with different defaults. It will be overwritten")
        super().__init__(default=default,
                         validator=validator,
                         docs=docs,
                         callback=self._mycallback,
                         precallback=precallback)
        self._name = ''
        self._base = ''
        self._configfile = ''
        self._persistent = False
        self._configPath = None
        self._callbacks = []
        self._loaded = False

        if name:
            self.name = name
        else:
            persistent = False
            load = False

        self.persistent = persistent

        if default is not None and load:
            self.load()

    @property
    def name(self) -> Opt[str]:
        return self._name

    @name.setter
    def name(self, name: str) -> None:
        if self._name:
            raise ValueError("Name has already been set")

        if name and name in self.registry:
            raise ValueError(f"Name {name} is already used")
        self._name = name
        base, configname = _parseName(name)
        self._base: str = base
        self._configFile: str = configname+".json"
        self.registry[name] = self

    @property
    def persistent(self) -> bool:
        return self._persistent

    @persistent.setter
    def persistent(self, value) -> None:
        self._persistent = value
        if value:
            if not self._name:
                raise ValueError("This ConfigDict can't be set to persistent without a name")
            self._ensureWritable()

    def _mycallback(self, key, value):
        for pattern, func in self._callbacks:
            if re.match(pattern, key):
                func(self, key, value)
        if self._persistent:
            self.save()

    def update(self, d: dict=None, **kws) -> None:
        if not d or kws:
            return
        kws.update(d)
        errormsg = self.checkDict(kws)
        if errormsg:
            logger.error(f"ConfigDict: {errormsg}")
            logger.error(
                    f"Reset the dict to a default by removing the file '{self.getPath()}'"
            )
            raise ValueError("dict is invalid")
        self._persistent, persistent = False, self._persistent
        super().update(kws)
        self._persistent = persistent
        if persistent:
            self.save()

    def copy(self) -> ConfigDict:
        return self.clone(name='', persistent=False, cloneCallbacks=False)

    def clone(self, name: str = '', persistent: bool=None, cloneCallbacks=False
              ) -> ConfigDict:
        if name == self._name or name in self.registry:
            raise ValueError(f"name {name} is already taken!")
        out = ConfigDict(default=self.default, validator=self._validator, docs=self._docs,
                         persistent=False, load=False, name=name)
        out.update(self)
        if name and persistent:
            out._persistent = True
        if cloneCallbacks and self._callbacks:
            for pattern, func in self._callbacks:
                out.registerCallback(func, pattern)
        return out

    def registerCallback(self, func, pattern=None) -> None:
        """
        Register a callback to be fired when a key matching the given pattern is
        changed. If no pattern is given, the function will be called for
        every key.

        Args:
            func: a function of the form (dict, key, value) -> None
                dict - this ConfigDict itself
                key - the key which was just changed
                value - the new value
            pattern: call func when pattern matches key
        """
        self._callbacks.append((pattern or r".*", func))

    def _ensureWritable(self) -> None:
        """ Make sure that we can serialize this dict to disk """
        folder, _ = os.path.split(self.getPath())
        if not os.path.exists(folder):
            os.makedirs(folder)

    def reset(self) -> None:
        super().reset()
        self.save()

    def save(self) -> None:
        """
        Normally a config doesn't need to be saved by the user,
        it is saved whenever it is modified.
        """
        path = self.getPath()
        logger.debug(f"Saving config to {path}")
        f = open(path, "w")
        json.dump(self, f, indent=True, sort_keys=True)

    def dump(self):
        """ Dump this config to stdout """
        print(str(self))

    def __str__(self) -> str:
        import tabulate
        header = f"Config: {self._name}\n"
        rows = []
        keys = sorted(self.keys())
        for k in keys:
            v = self[k]
            info = []
            lines = []
            choices = self.getChoices(k)
            if choices:
                choicestr = ", ".join(str(ch) for ch in choices)
                if len(choicestr)>self._helpwidth:
                    choiceslines = textwrap.wrap(choicestr, self._helpwidth)
                    lines.extend(choiceslines)
                else:
                    info.append(choicestr)
            keyrange = self.getRange(k)
            if keyrange:
                info.append(f"between {keyrange}")
            typestr = self.getTypestr(k)
            info.append(typestr)
            valuestr = str(v)
            rows.append((k, valuestr, " | ".join(info)))
            doc = self.getDoc(k)
            if doc:
                doclines = textwrap.wrap(doc, self._helpwidth)
                lines.extend(doclines)
            for line in lines:
                rows.append(("", "", line))
        return header+tabulate.tabulate(rows)

    def getPath(self) -> str:
        """ Return the path this dict will be saved to """
        if self._configPath is not None:
            return self._configPath
        self._configPath = path = configPathFromName(self._name)
        return path

    def edit(self, wait_on_modified=False) -> ConfigDict:
        """
        Edit (and reload) this config in an external application

        Args:
            wait_on_modified: if True, we wait until the file is modified.
                Otherwise the user must click the pop-up dialog to signal
                that the editing is finished
        """
        self.save()
        _openInEditor(self.getPath())
        if wait_on_modified:
            _wait_on_file_modified(self.getPath())
        else:
            _waitForClick()
        self.load()
        return self

    def load(self) -> None:
        """
        Read the saved config, update self. This is used internally but it can be usedful
        if the file is changed externally and no monitoring is activated

        * If no saved config (not present or unreadable)
            * if default was given:
                * use default
            * otherwise:
                * if saved config is unreadable, raise JSONDecodeError
                * if saved config not present, raise FileNotFoundError
        """
        configpath = self.getPath()
        if not os.path.exists(configpath):
            if self.default is None:
                logger.error(
                        "No written config found, but default was not set")
                raise FileNotFoundError(f"{configpath} not found")
            logger.debug("Using default config")
            confdict = self.default
        else:
            logger.debug(f"Reading config from disk: {configpath}")
            try:
                confdict = json.load(open(configpath))
                if self.default is None:
                    raise ValueError("Default config not set")
            except json.JSONDecodeError:
                error = sys.exc_info()[0]
                logger.error(f"Could not read config {configpath}: {error}")
                if self.default is not None:
                    logger.debug(
                            "Couldn't read config. Using default as fallback")
                    confdict = self.default
                else:
                    logger.error(
                            "Couldn't read config. No default given, we give up")
                    raise

        # only keys in default should be accepted, but keys in the read
        # config should be discarded with a warning
        keysOnlyInRead = confdict.keys()-self.default.keys()
        if keysOnlyInRead:
            logger.warning(f"ConfigDict {self._name}, saved at {configpath}")
            logger.warning(
                    "There are keys defined in the saved"
                    " config which are not present in the default config. They will"
                    " be skipped:")
            logger.warning(f"   {keysOnlyInRead}")

        # merge strategy:
        # * if a key is shared between default and read dict, read dict has priority
        # * if a key is present only in default, it is added
        confdict = _mergeDicts(confdict, self.default)
        self.checkDict(confdict)
        super().update(confdict)
        self._loaded = True

def _makeName(configname: str, base: str = None) -> str:
    if base is not None:
        return f"{base}:{configname}"
    else:
        return f":{configname}"


def _mergeDicts(readdict: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    sharedkeys = readdict.keys() & default.keys()
    for key in sharedkeys:
        out[key] = readdict[key]
    onlyInDefault = default.keys() - readdict.keys()
    for key in onlyInDefault:
        out[key] = default[key]
    return out


def _parseName(name: str) -> Tuple[str, Opt[str]]:
    """
    Returns (configname, base) (which can be None)
    """
    if ":" not in name:
        base = None
        configname = name
    else:
        base, *rest = name.split(":")
        configname = ".".join(rest)
        if not base:
            base = None
    return base, configname


def _isValidName(name: str) -> bool:
    return re.fullmatch(r"[a-zA-Z0-9\.\:_]+", name) is not None


def _normalizeName(name: str) -> str:
    """
    Originally a name would be of the form project:name,
    later on we enabled / and . to act as path separator

    """
    if "/" in name:
        return name.replace("/", ":")
    elif "." in name:
        return name.replace(".", ":")
    return name


def _checkName(name):
    if not _isValidName(name):
        raise ValueError(
                f"{name} is not a valid name for a config."
                " It should contain letters, numbers and any of '.', '_', ':'")


def getConfig(name: str) -> Opt[ConfigDict]:
    """
    Retrieve a previously created ConfigDict.

    Args:
        name: the unique id of the configuration, as passed to ConfigDict

    Returns:
        the ConfigDict, if found. None otherwise.

    """
    name = _normalizeName(name)
    _checkName(name)
    return ConfigDict.registry.get(name)


def activeConfigs() -> Dict[str, ConfigDict]:
    """
    Returns a dict of active configs
    """
    return ConfigDict.registry.copy()


def _removeConfigFromDisk(name: str) -> bool:
    """
    Remove the given config from disc, returns True if it was found and removed,
    False otherwise
    """
    configpath = configPathFromName(name)
    if os.path.exists(configpath):
        os.remove(configpath)
        return True
    return False


def configPathFromName(name: str) -> str:
    name = _normalizeName(name)
    userconfigdir = appdirs.user_config_dir()
    base, configname = _parseName(name)
    configfile = configname+".json"
    if base is not None:
        configdir = os.path.join(userconfigdir, base)
    else:
        configdir = userconfigdir
    return os.path.join(configdir, configfile)
