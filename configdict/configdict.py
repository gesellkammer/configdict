"""
CheckedDict
-----------

A dictionary based on a default prototype. A :class:`CheckedDict` can only define
``key:value`` pairs which are already present in the default. It is possible to
define a docstring for each key and different restrictions for the values
regarding possible values, ranges and type. A CheckedDict is useful for
configuration settings.

If no mutable values are used, a CheckedDict is hashable

ConfigDict
----------

Based on :class:`CheckedDict`, a :class:`ConfigDict` is a persistent, unique dictionary. It is
saved under the config folder determined by the OS and it is updated with each
modification. It is useful for implementing configuration of a module / library
/ app, where there is a default/initial state and the user needs to be able to
configure global settings which must be persisted between sessions (similar to
the settings in an application)

Example
~~~~~~~

.. code::

    from configdict import ConfigDict

    config = ConfigDict("myproj.subproj")
    config.addKey("keyA", 10, doc="documentaion of keyA")
    config.addKey("keyB", 0.5, range=(0, 1))
    config.addKey("keyC", "blue", choices=("blue", "red"), doc="documentation of keyC")
    config.load()

Alternativaly, a :class:`ConfigDict` or a :class:`CheckedDict` can be built
via a context manager::

    with ConfigDict("plotting") as cfg:
        # While building a config, __call__ is equivalent to addKey
        cfg('backend', 'matplotlib', choices={'matlotlib'})
        cfg('spectrogram.figsize', (24, 8))
        cfg('spectrogram.maxfreq', 12000,
          doc="Highest frequency in a spectrogram")
        cfg('spectrogram.window', 'hamming', choices={'hamming', 'hanning'})
        # no need to call .load, it is called automatically

A :class:`ConfigDict` can be created all at once

.. code::

    config = ConfigDict("myapp",
        default = {
            'font-size': 10.0,
            'font-family': "Monospace",
            'port' : 9100,
        },
        validator = {
            'font-size::range' : (8, 24),
            'port::range' : (9000, 65000),
            'font-family::choices' : {'Roboto', 'Monospace'},
            'port': lambda cfg, port: checkPortAvailable(port)
        },
        docs = {
            'port': 'The port number to listen to',
            'font-size': 'The size of the font, in pixels'
        }
    )


This will create the dictionary and load any persisted version. Any saved
modifications will override the default values. Whenever the user changes any
value (via ``config[key] = newvalue``) the dictionary will be saved.

In all other respects a :class:`ConfigDict` behaves like a normal dictionary.

"""
from __future__ import annotations

import appdirs
import os
import json

import yaml
import logging
import sys
import re
import textwrap
import tempfile
from functools import cache
from types import FunctionType
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import Optional, Any, Union, Callable, TypeVar, Set
    validatefunc_t = Callable[[dict, str, Any], bool]
    T = TypeVar("T", bound="CheckedDict")

__all__ = ("CheckedDict",
           "ConfigDict",
           "getConfig",
           "activeConfigs",
           "configPathFromName")

logger = logging.getLogger("configdict")


_UNKNOWN = object()


_editHeaderWatch = (r'''#  ****************************************************
#  *   Edit this file to modify the configuration     *
#  *   When you are finished editing, save the file   *
#  ****************************************************
''')

_editHeaderPopup = (r'''#  **********************************************************
#  *  Edit this file to modify the configuration            *
#  *  Click OK on the popup dialog to finish the opeartion  *
#  **********************************************************
''')


def sortNatural(seq: list, key:Callable[[Any], str]=None) -> list:
    """
    Sort a string sequence naturally

    Sorts the sequence so that 'item1' and 'item2' are before 'item10'

    Args:
        seq: the sequence to sort
        key: a function to convert an item in seq to a string

    Examples
    ~~~~~~~~

    >>> seq = ["e10", "e2", "f", "e1"]
    >>> sorted(seq)
    ['e1', 'e10', 'e2', 'f']
    >>> sortNatural(seq)
    ['e1', 'e2', 'e10', 'f']

    >>> seq = [(2, "e10"), (10, "e2")]
    >>> sortNatural(seq, key=lambda tup:tup[1])
    [(10, 'e2'), (2, 'e10')]
    """
    def convert(text: str):
        return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key: str):
        return [convert(c) for c in re.split('([0-9]+)', key)]

    if key is not None:
        return sorted(seq, key=lambda x: alphanum_key(key(x)))
    return sorted(seq, key=alphanum_key)


def _asChoiceStr(x) -> str:
    return f"'{x}'" if isinstance(x, str) else str(x)


def _makeReplacer(conditions: dict) -> Callable:
    """
    Create a function to replace many subtrings at once

    Args:
        conditions: a dictionary mapping a string to its replacement

    Example::

        >>> replacer = makeReplacer({"&":"&amp;", " ":"_", "(":"\\(", ")":"\\)"})
        >>> replacer("foo & (bar)")
        "foo_&amp;_\(bar\)"

    """
    rep = {re.escape(k): v for k, v in conditions.items()}
    pattern = re.compile("|".join(rep.keys()))
    return lambda txt: pattern.sub(lambda m: rep[re.escape(m.group(0))], txt)


_keyNormalizer = _makeReplacer({'.': '', '_': '', '-': ''})


@cache
def normalizeKey(key: str) -> str:
    return _keyNormalizer(key.lower())


def _yamlComment(doc: Optional[str],
                 default: Any,
                 choices: Optional[set],
                 valuerange: Optional[tuple[float, float]],
                 valuetype: Optional[str],
                 maxwidth=80) -> str:
    """
    This generated the yaml comments used when saving the config to yaml

    Args:
        doc: documentation for this key
        default: the default value
        choices: choices possible to this value
        valuerange: a tuplet indicating a valid range for this value
        valuetype: the type as string
        maxwidth: the max. width of one line

    Returns:
        the generated comment as a string. It might contain multiple lines
    """
    if all(_ is None for _ in (doc, default, choices, valuerange, valuetype)):
        return ""
    """
    # this is the documentation for bla
    # default: xxx, choices: 10, 20, 30, type: int, range: 0.0 - 1.0
    """
    lines = []
    infoparts = [f"default: {default}"]
    if doc:
        if len(doc) < maxwidth:
            lines.append(f"# {doc}")
        else:
            lines.extend("# " + l for l in textwrap.wrap(doc, maxwidth))
    if choices:
        valuetype = None
    if valuetype:
        infoparts.append(f"type: {valuetype}")
    if choices:

        infoparts.append(f"choices: {', '.join(map(str, choices))}")
    if valuerange:
        infoparts.append(f"range: {valuerange[0]} - {valuerange[1]}")
    if infoparts:
        lines.append("# ** " + ", ".join(infoparts))
    return "\n".join(lines)


def _yamlValue(value) -> str:
    if isinstance(value, tuple):
        value = list(value)
    s = yaml.dump(value, default_flow_style=True)
    return s.replace("\n...\n", "")


def _typeName(t: Union[str, type, tuple[type,...]]) -> str:
    if isinstance(t, str):
        return t
    elif isinstance(t, type):
        return t.__name__
    elif isinstance(t, tuple):
        return " | ".join(v.__name__ for v in t)
    else:
        raise TypeError(f"Expected a str, type or tuple of types, got {t}")

def _asRstLinkKey(key: str) -> str:
    return key.replace(".", "_").replace(" ", "").lower()

def _asYaml(d: dict[str, Any],
            doc: dict[str, str],
            default: dict[str, Any],
            validator: dict[str, Any] = None,
            keys: list[str] = None,
            advancedPrefix = '.'
            ) -> str:
    lines = []

    # detect if keys have advanced keys and they are all at the end

    if keys:
        items = [(k, d[k]) for k in keys]
    else:
        items = list(d.items())

    firstAdvanced = next((i for i, item in enumerate(items) if item[0].startswith(advancedPrefix)), None)
    if firstAdvanced is not None and all(k.startswith(advancedPrefix) for k, v in items[firstAdvanced:]):
        addAdvancedSeparator = True
    else:
        addAdvancedSeparator = False

    for key, value in items:
        if addAdvancedSeparator and key.startswith(advancedPrefix):
            addAdvancedSeparator = False
            lines.append("\n"
                         "#####################################################\n"
                         "#                 Advanced Keys                     #\n"
                         "#####################################################\n")

        choices = validator.get(f"{key}::choices")
        valuerange = validator.get(f"{key}::range")
        valuetype = validator.get(f"{key}::type")
        valuetypestr = type(value).__name__ if valuetype is None else _typeName(valuetype)
        comment = _yamlComment(doc=doc.get(key), default=default.get(key),
                               choices=choices, valuerange=valuerange,
                               valuetype=valuetypestr)
        lines.append(comment)
        l = f"{key}: {_yamlValue(value)}"
        lines.append(l)
        if not l.endswith("\n"):
            lines.append("")
    return "\n".join(lines)


def _htmlTable(rows: list, headers, maxwidths=None, rowstyles=None) -> str:
    parts = []
    _ = parts.append
    _("<table>")
    _("<thead>")
    _("<tr>")
    if maxwidths is None:
        maxwidths = [0] * len(headers)
    if rowstyles is None:
        rowstyles = [None] * len(headers)
    for colname in headers:
        _(f'<th style="text-align:left">{colname}</th>')
    _("</tr></thead><tbody>")
    for row in rows:
        _("<tr>")
        for cell, maxwidth, rowstyle in zip(row, maxwidths, rowstyles):
            if rowstyle is not None:
                cell = f'<{rowstyle}>{cell}</{rowstyle}>'
            if maxwidth > 0:
                _(f'<td style="text-align:left;max-width:{maxwidth}px;">{cell}</td>')
            else:
                _(f'<td style="text-align:left">{cell}</td>')
        _("</tr>")
    _("</tbody></table>")
    return "".join(parts)


def _checkDocs(docs: dict[str, str], keys: set[str]) -> bool:
    ok = True
    for key in docs.keys():
        if key not in keys:
            likely = _bestMatches(key, keys, limit=16, minpercent=60)
            logger.warning(f"Key {key} not defined. Did you mean {likely}?. \nPossible keys: {keys}")
            ok = False
    return ok


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
        raise KeyError(f"The validator dict has keys not present "
                       f"in the defaultdict ({notpres})")
    v = {}
    for key, value in validatordict.items():
        if key.endswith('::choices') and isinstance(value, (list, tuple)):
            value = set(value)
        v[key] = value
    return v


def _isfloaty(value) -> bool:
    return isinstance(value, (int, float)) or hasattr(value, '__float__')


def _openInStandardApp(path: str) -> None:
    """
    Open path with the app defined to handle it by the user
    at the os level (xdg-open in linux, start in win, open in osx)
    """
    import subprocess
    platform = sys.platform
    if not os.path.exists(path):
        raise RuntimeError(f"Trying to open '{path}', but file does not exist")

    if platform == 'linux':
        subprocess.call(["xdg-open", path])
    elif platform == "win32":
        os.startfile(path)
    elif platform == "darwin":
        subprocess.call(["open", path])
    else:
        raise RuntimeError(f"platform {platform} not supported")


def _notify(title: str, msg: str) -> None:
    import subprocess
    if sys.platform == "linux":
        print(f"**Notify** {title}: {msg}")
        subprocess.call(['notify-send', title, msg])


def _waitOnFileModified(path:str, timeout:float=None, notification:str='') -> bool:
    try:
        from watchdog.observers import Observer
        from watchdog.events import PatternMatchingEventHandler
    except ImportError:
        logger.warning("watchdog is needed to be able to wait on file events. "
                       "Install via `pip install watchdog`")
        _waitForClick()
        return False

    directory, base = os.path.split(path)
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
        timeout = 60 * 20  # 20 minutes
    observer.join(timeout)
    if notification:
        if "::" in notification:
            title, body = notification.split("::")
        else:
            title, body = "Edit", notification
        _notify(title, body)
    return modified


def _showInfoDialog(msg: str, title: str = None) -> None:
    """
    Creates a simple confirmation dialog box

    Args:
        msg: the message to display
        title: a title for the window
    """
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    window.wm_withdraw()
    messagebox.showinfo(title, msg)
    window.destroy()


def _waitForClick(title:str=None):
    _showInfoDialog("Click OK when finished editing", title=title)


def _openInEditor(cfg: str) -> None:
    _openInStandardApp(cfg)


def _bestMatches(s: str, options: list[str], limit:int, minpercent:int, lengthMatchPercent=None) -> list[str]:
    from fuzzywuzzy import process
    possibleChoices = process.extract(s, options, limit=limit)
    if lengthMatchPercent:
        lens = len(s)
        lengthdiff = lens * (1 - lengthMatchPercent/100)
        minlength = lens - lengthdiff
        maxlength = lens + lengthdiff
        return [choice for choice, percent in possibleChoices
                if percent >= minpercent and minlength <= len(choice) <= maxlength]
    else:
        return [choice for choice, percent in possibleChoices
                if percent >= minpercent]


INVALID = object()


def _forceHash(values) -> int:
    hashes = []
    for value in values:
        if isinstance(value, list):
            hashes.append(hash(tuple(value)))
        elif isinstance(value, dict):
            raise ValueError(f"Dicts cannot be forced to have a hash ({value}")
        else:
            hashes.append(hash(value))
    return hash(tuple(hashes))


class CheckedDict(dict):
    """
    A dictionary which checks that the keys and values are valid
    according to a default dict and a validator. In a :class:`CheckedDict`,
    only keys are allowed which are already present in the default given.

    Args:
        default: a dict will all default values. A config can accept only
            keys which are already present in the default

        validator: a dict containing choices and types for the keys in the
            default. Given a default like: ``{'keyA': 'foo', 'keyB': 20, 'keyC': 0.5}``,
            a validator could be::

                {'keyA::choices': ['foo', 'bar'],
                 'keyB::type': float,
                 'keyB': lambda d, value: value > d['keyC'] * 10
                 'keyC::range': (0, 1)
                }

            choices can be defined lazyly by giving a lambda which returns a list
            of possible choices

        docs: a dict containing help lines for keys defined in default
        callback: function ``(key, value) -> None``. This function is called **after**
            the modification has been done.
        precallback: function ``(key, value) -> newvalue``. If given, a precallback intercepts
            any change and can modify the value or return INVALID to prevent the modification
        strict: if False keys are case and punktuation insensitive, meaning that
            a key like 'foo.barBaz' will also be matched by 'foo_bar_baz' or 'foo_barbaz'

    Example
    =======

    .. code::

        from configdict import *
        default = {
            'color': '#FF0000',
            'size': 10,
            'name': ''
        }

        validator = {
            'size::range': (6, 30),
            'color': lambda d, value: iscolor(value)
        }

        checked = CheckedDict(default, validator=validator)
    """

    def __init__(self,
                 default: dict[str, Any] = None,
                 validator: dict[str, Any] = None,
                 docs: dict[str, str] = None,
                 callback: Callable[[str, Any], None] = None,
                 precallback=None,
                 autoload=True,
                 strict=True,
                 advancedPrefix='.') -> None:

        self.default = default if default else {}
        self._validator = _checkValidator(validator, default) if validator else {}
        self._docs = docs if docs else {}
        self._allowedkeys = set(default.keys()) if default else set()
        self._precallback = precallback
        self._callback = callback
        self._building = False
        self._normalizedKeys: dict[str, str] = {}
        self._bypass = False
        self._advancedPrefix = advancedPrefix

        if docs:
            _checkDocs(docs, self._allowedkeys)

        if self.default:
            if autoload:
                self.load()
            if not strict:
                self._normalizedKeys = {normalizeKey(k): k for k in self.default.keys()}

    def __hash__(self) -> int:
        keyshash = hash(tuple(self.keys()))
        try:
            valueshash = hash(tuple(self.values()))
        except:
            logger.debug(f"Some values are unhashable, using unsafe hash ({self.values()}")
            valueshash = id(self)
        return hash((len(self), keyshash, valueshash, hash(self._precallback), hash(self._callback)))

    def _changed(self) -> None:
        self._allowedkeys = set(self.default.keys())

    @staticmethod
    def normalizeKey(key: str) -> str:
        return normalizeKey(key)

    def copy(self: T) -> T:
        """
        Create a copy of this dict
        """
        out = self.__class__(default=self.default, validator=self._validator, docs=self._docs,
                             precallback=self._precallback, callback=self._callback, autoload=False)
        out._bypass = True
        out.update(self)
        out._bypass = False
        return out

    def clone(self: T, updates: dict = None, **kws) -> T:
        """
        Clone self with modifications

        Args:
            updates: a dict with updated values for the clone dict
            kws: any keyworg arg will be used to update the resulting dict

        Returns:
            the cloned dict

        Examples
        ~~~~~~~~

            >>> import configdict
            >>> d = configdict.CheckedDict(default={'A': 10, 'B': 20, 'C':30})
            >>> d2 = d.clone({'B':21}, C=31)
            >>> d2
            {'A': 10, 'B': 21, 'C': 31)
        """
        out = self.copy()
        if updates:
            out.update(updates)
        if kws:
            out.update(kws)
        return out

    def makeDefault(self: T) -> T:
        """
        Create a version of this class with all values set to the default
        """
        return self.clone(updates=self.default)

    def diff(self, other: T | None = None) -> dict:
        """
        Get a dict containing keys:values which differ from the default or from another dict

        Args:
            other: if given, another dict which this is compared against

        Returns:
            a dict containing key: value pairs where self differs from other
        """
        if other is None:
            other = self.default
        return {k: v for k, v in self.items()
                if v != other.get(k, _UNKNOWN)}

    def __call__(self, key: str, value: Any, type=None, choices=None,
                 range: tuple[Any, Any] = None, doc: str = '',
                 validatefunc: validatefunc_t = None) -> None:
        if not self._building:
            raise RuntimeError("Not inside a context manager context")
        self.addKey(key=key, value=value, type=type, choices=choices,
                    range=range, doc=doc, validatefunc=validatefunc)

    def addKey(self,
               key: str,
               value: Any,
               type: Union[type, tuple[type,...]] = None,
               choices: Union[Set, tuple] = None,
               range: tuple[Any, Any] = None,
               validatefunc: validatefunc_t = None,
               doc: str = None) -> None:
        """
        Add a ``key: value`` pair to the default settings.

        This is used when building the default config item by item (see example).
        After adding all new keys it is necessary to call :meth:`ConfigDict.load()`

        Example
        =======

        .. code::

            cfg = ConfigDict("foo", load=False)
            # We define a default step by step
            cfg.addKey("width", 100, range=(50, 150))
            cfg.addKey("color", "red", choices=("read", "blue", "green"))
            cfg.addKey("height",
                       doc="Height should be higher than width",
                       validatefunc=lambda cfg, key, height: height > cfg['width'])
            # Now update the dict with the newly defined default and any
            # saved version
            cfg.load()

        Args:
            key: a string key
            value: a default value
            type: the type accepted, as passed to isinstance (can be a tuple)
            choices: a set/tuple of possible values
            range: a (min, max) tuple defining an allowed range for this value
            validatefunc: a function ``(config: dict, key:str, value) -> bool``, should return
                `True` if value is valid for `key` or False otherwise
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
        if validatefunc:
            assert callable(validatefunc), f"Validate function ({validatefunc}) is not callable for key: {key}"
            validator[key] = validatefunc
        if doc:
            self._docs[key] = doc

    def __getitem__(self, key: str):
        if (value := dict.get(self, key, _UNKNOWN)) is not _UNKNOWN:
            return value

        if self._normalizedKeys and (key2 := self._normalizedKeys.get(normalizeKey(key))):
            return dict.__getitem__(self, key2)

        nearest = self._bestMatches(key, limit=8)
        raise KeyError(f"key '{key}' not known. Did you mean {nearest}?\n"
                       f"Possible keys: {sorted(self.keys())}")

    def __setitem__(self, key: str, value) -> None:
        if self._bypass:
            dict.__setitem__(key, value)
            return

        if key not in self._allowedkeys:
            if self._normalizedKeys and (normkey := self._normalizedKeys.get(normalizeKey(key))):
                key = normkey
            else:
                mostlikely = self._bestMatches(key=key, limit=8)
                msg = f"Unknown key {key}. Did you mean {', '.join(mostlikely)}?"
                raise KeyError(msg)

        oldvalue = self.get(key)
        if oldvalue is not None and oldvalue == value:
            return
        if self._validator:
            errormsg = self.checkValue(key, value)
            if errormsg:
                raise ValueError(errormsg)
        if self._precallback:
            newvalue = self._precallback(self, key, oldvalue, value)
            if newvalue is not INVALID:
                value = newvalue

        super().__setitem__(key, value)

        if self._callback is not None:
            self._callback(key, value)

    def _bestMatches(self, key: str, limit=16, minpercent=60):
        return _bestMatches(key, list(self._allowedkeys), limit=limit, minpercent=minpercent)

    def load(self) -> None:
        """
        Update any undefined key in self with the default value

        Example
        ~~~~~~~

        ::
            from configdict import *
            config = CheckedConfig()
            config.addKey(...)
            config.addKey(...)
            ...
            config.load()
            # Now config is fully defined

        """
        if not self.default:
            raise ValueError("This dict has no default")
        if len(self) == 0:
            super().update(self.default)
        else:
            d = self.default.copy()
            d.update(self)
            self.update(d)
        self._loaded = True

    def checkDict(self, d: dict) -> str:
        """
        Check if dict `d` can be used to update self

        Args:
            d (dict): a dict which might update self

        Returns:
            An error message if `d` has any invalid `key` or `value`,
            "" if everything is ok

        """
        invalidkeys = [key for key in d if key not in self.default]
        if invalidkeys:
            return f"Some keys are not valid: {invalidkeys}"
        if self._validator:
            for k, v in d.items():
                errormsg = self.checkValue(k, v)
                if errormsg:
                    return errormsg
        return ""

    def getValidateFunc(self, key:str) -> Optional[validatefunc_t]:
        """
        Returns a function to validate a value for ``key``

        A validate function has the form ``(config, value) -> bool``

        Args:
            key (str): the key to query for a validate function

        Returns:
            The validate function, or None

        """
        func = self._validator.get(key, None)
        assert func is None or callable(func), f"Validate func should be callable for key {key}, got {func}"
        return func

    def getChoices(self, key: str) -> Optional[list]:
        """
        Return a seq. of possible values for key ``k`` or ``None``
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

    def getDoc(self, key: str) -> Optional[str]:
        """ Get documentation for key (if present) """
        if self._docs:
            return self._docs.get(key)

    def checkValue(self, key: str, value) -> Optional[str]:
        """
        Check if value is valid for key

        This is only possible if a validator was set

        Args:
            key: the key to check
            value: the value to check according to the contraints defined
                for the key (range, type, etc)

        Returns:
            None if the value is acceptable for the key, an error message
            otherwise

        Example
        =======

        .. code::

            error = config.checkType(key, value)
            if error:
                print(error)
        """
        if not self._validator:
            logger.debug(f"Validator not set, cannot check value {value} (key '{key}')")
            return

        for validatortype in self.getValidateFunc(key):
            if validatortype == 'choices':
                choices = self.getChoices(key)
                if choices is not None and value not in choices:
                    return f"key {key} should be one of {choices}, got {value}"
            elif validatortype == 'func':
                error = self.getValidateFunc(key)(self, key, value)
                if error is False:
                    return f"{value} is not valid for key {key}"
                elif isinstance(error, str) and error:
                    return f"{value} is not valid for key {key}: {error}"
            elif validatortype == 'type':
                t = self.getType(key)
                if t == float:
                    if not _isfloaty(value):
                        return f"Expected floatlike for key {key}, got {type(value).__name__}"
                elif t == str:
                    if not isinstance(value, (bytes, str)):
                        return f"Expected str or bytes for key {key}, got {type(value).__name__}"
                elif not isinstance(value, t):
                    return f"Expected {t.__name__} for key {key}, got {type(value).__name__}"
            elif validatortype == 'range':
                if (r := self.getRange(key)) and not (r[0] <= value <= r[1]):
                    return f"Value for key {key} should be within range {r}, got {value}"
        return None

    @cache
    def getValidatorTypes(self, key: str) -> list[str]:
        """
        Return the validator types for a given key

        A validator type for a given key can be a choices validator, where a set of
        possible values is given for a given key; it can be a range, where the
        value must be within a given range; a type, where a value must be of
        a certain type; or a function, which must return True if the value
        is valid, or False or an error message as string if the value is invalid

        Args:
            key: the key to query

        Returns:
            a list of validator types, where each item is one of 'choices',
            'range', 'type', 'func'
        """
        validators = []
        if f"{key}::choices" in self._validator:
            validators.append('choices')
        if f"{key}::range" in self._validator:
            validators.append('range')
        if key in self._validator:
            validators.append('func')
        if f"{key}::type" in self._validator:
            validators.append('type')
        return validators

    def getRange(self, key: str) -> Optional[tuple]:
        """
        Returns the valid range for this key's value, if specified.

        Args:
            key: the key to get the range from.

        Returns:
            the range of values allowed for this key, or None if there is no
            range defined for this key.

        Raises KeyError if the key is not present
        """
        if key not in self._allowedkeys:
            raise KeyError(f"{key} is not a valid key")
        if not self._validator:
            logger.debug("getRange: validator not set")
            return None
        return self._validator.get(key+"::range", None)

    def getType(self, key: str) -> Union[type, tuple[type,...]]:
        """
        Returns the expected type for key's value

        Args:
            key: the key to query

        .. note::

            All numbers are reduced to type float, all strings are of type str,
            otherwise the type of the default value, which can be a collection
            like a list or a dict

        See Also: :meth:`checkValue`
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
        """
        The same as `.getType` but returns a string representation of the type

        Args:
            key: the key to query
        """
        t = self.getType(key)
        if isinstance(t, tuple):
            return "("+", ".join(x.__name__ for x in t)+")"
        else:
            return t.__name__

    def reset(self) -> None:
        """
        Resets the config to its default (inplace)
        """
        self.clear()
        self.update(self.default)

    def _normalizeDict(self, d: dict) -> dict:
        out = {}
        keys = self.keys()
        for k, v in d.items():
            if k in keys:
                out[k] = v
            elif k2:=self._normalizedKeys.get(normalizeKey(k)):
                out[k2] = v
            else:
                raise KeyError(f"Unsupported key: {k}")
        return out

    def update(self, d: dict=None, **kws) -> None:
        """
        Update ths dict with `d` or any key:value pair passed as keyword
        """
        if d:
            errormsg = self.checkDict(d)
            if errormsg:
                raise ValueError(f"dict is invalid: {errormsg}")
            super().update(d)
        if kws:
            for k, v in kws.items():
                if k not in self._allowedkeys and self._normalizedKeys:
                    k2 = self._normalizedKeys.get(normalizeKey(k))
                    if k2:
                        del kws[k]
                        kws[k2] = v
            errormsg = self.checkDict(kws)
            if errormsg:
                raise ValueError(f"invalid keywords: {errormsg}")
            super().update(kws)

    def updated(self:T, d: dict=None, **kws) -> T:
        """
        The same as :meth:`~CheckedDict.update`, but returns self
        """
        self.update(d, **kws)
        return self

    def override(self, key: str, value, default=None) -> None:
        """
        The same as `value if value is not None else config.get(key, default)`
        """
        return value if value is not None else self.get(key, default)

    def asYaml(self, sortKeys=False) -> str:
        """
        Returns this dict as yaml str, with comments, defaults, etc.
        """
        if sortKeys:
            keys = self._sortedKeys()
        else:
            keys = list(self.keys())
            keys.sort(key=lambda key: int(key.startswith(self._advancedPrefix)))
            return _asYaml(self, doc=self._docs, validator=self._validator,
                       default=self.default, keys=keys)

    def __enter__(self):
        self._building = True
        return self

    def __exit__(self, *args, **kws):
        self._building = False
        self.load()

    def edit(self, waitOnModified=True, sortKeys=False) -> None:
        configfile = tempfile.mktemp(suffix=".yaml")
        header = _editHeaderWatch if waitOnModified else _editHeaderPopup
        self._saveAsYaml(configfile, header=header, sortKeys=sortKeys)
        _openInEditor(configfile)
        if waitOnModified:
            try:
                _waitOnFileModified(configfile)
            except KeyboardInterrupt:
                logger.debug("Editing aborted")
                return
        else:
            _waitForClick(title=self.name)
        self.load(configfile)

    def _saveAsYaml(self, path: str, header: str = '', sortKeys=False, separateAdvancedKeys=True) -> None:
        yamlstr = self.asYaml(sortKeys=sortKeys)
        folder = os.path.split(path)[0]
        os.makedirs(folder, exist_ok=True)
        with open(path, "w") as f:
            if header:
                f.write(header)
                f.write("\n")
            f.write(yamlstr)
        if not os.path.exists(path):
            raise RuntimeError(f"Could not save config to file '{path}', file not found")

    @cache
    def _sortedKeys(self) -> list[str]:
        keys = list(self.keys())
        keys.sort()
        keys.sort(key=lambda k: int(k.startswith(self._advancedPrefix)))
        return keys

    def _repr_html_(self) -> str:
        parts = [f'<div><h4>{type(self).__name__}</h4>']
        parts.append("<br>")
        rows = []
        keys = self._sortedKeys()
        for k in keys:
            v = self[k]
            rows.append((k, str(v), self._infoStr(k), self.getDoc(k)))
        table = _htmlTable(rows, headers=('Key', 'Value', 'Type', 'Descr'), maxwidths=[0, 0, 150, 400],
                           rowstyles=('strong', 'code', None, None))
        parts.append(table)
        parts.append("</div>")
        return "".join(parts)


def _loadJson(path:str) -> Optional[dict]:
    try:
        return json.load(open(path))
    except json.JSONDecodeError:
        error = sys.exc_info()[0]
        logger.error(f"Could not read config {path}: {error}")
        logger.debug("Using default as fallback")


def _loadYaml(path: str, fail=False) -> Optional[dict]:
    try:
        with open(path) as f:
            return yaml.load(f, Loader=yaml.SafeLoader)
    except Exception as e:
        err = sys.exc_info()[0]
        logger.error(f"Could not read config {path}: {err}")
        if fail:
            raise e


def _loadDict(path: str) -> Optional[dict]:
    fmt = os.path.splitext(path)[1]
    if fmt == ".json":
        return _loadJson(path)
    elif fmt == ".yaml":
        return _loadYaml(path, fail=False)
    else:
        raise ValueError(f"format {fmt} unknown, supported formats: json, yaml")


class ConfigDict(CheckedDict):
    """
    This is an optionally persistent dictionary used for configuration.

    It is saved under the config folder determined by
    the OS (and is thus OS dependent). In persistent mode no two instances of the same
    config can coexist.

    Args:
        name: a str of the form ``prefix.name`` or ``prefix/name``
            (these are the same) or simply ``name`` if this is an
            isolated configuration. The
            data will be saved at ``$USERCONFIGDIR/{prefix}/{name}.{fmt}`` if
            prefix is given, or ``$USERCONFIGDIR/{name}.{fmt}``.
            For instance, in Linux a config with a name "myproj.myconfig" and
            a yaml format will be saved to "~/.config/mydir/myconfig.yaml"

        default: a dict with all default values. A config can accept only
            keys which are already present in the default. This argument can be
            None if the config is built successively via :meth:`ConfigDict.addKey`
            (see example below) but the dict is not usable until all the keys have
            been added and the user calls :meth:`ConfigDict.load` explicitely

        validator: a dict containing choices, types and/or ranges for the keys in the
            default. Given a default like: ``{'keyA': 'foo', 'keyB': 20}``,
            a validator could be::

                {
                  'keyA::choices': ['foo', 'bar'],
                  'keyB::type': float,
                  'keyB::range': (10, 30)
                }

            Choices can be defined lazyly by giving a lambda

        docs: a dict containing documentation for each key

        persistent: if True, any change to the dict will be automatically saved.
            Otherwise a dict can be saved manually via :meth:`ConfigDict.save`

        load: if True, the saved version will be loaded after creation. 
            This is disabled if no default dict is given. This is the case when building
            the default after creation - :meth:`ConfigDict.load` should be called
            manually in this case (see example).

        precallback: function `(dict, key, oldvalue, newvalue) -> None|newvalue`,
            If given, it is called *before* the modification is done. This function
            should return **None** to allow modification, **any value** to modify the 
            value, or **raise ValueError** to stop the transaction

        sortKeys: if True, keys are sorted whenever the dict is saved/edited.
        advancedPrefix: keys with this prefix are marked as advanced. Whenever the dict
            is displayed or edited, these keys appear after all the other keys


    Example
    =======

    .. code::

        # No default given. The default is built by adding keys subsequently.
        # load needs to be called to end the declaration
        # This method is somewhat similar to ArgParse
        config = ConfigDict("myproj.subproj")
        config.addKey("keyA", 10, doc="documentaion of keyA")
        config.addKey("keyB", 0.5, range=(0, 1))
        config.addKey("keyC", "blue", choices=("blue", "red"),
                      doc="documentation of keyC")
        config.load()

        # Alternatively, a config can be built within a context manager. 'load'
        is called when exiting the context:

        with ConfigDict("maelzel.snd.plotting") as conf
            conf.addKey('backend', 'matplotlib', choices={'matlotlib'})
            conf.addKey('spectrogram.colormap', 'inferno', choices=_cmaps)
            conf.addKey('samplesplot.figsize', (24, 4))
            conf.addKey('spectrogram.figsize', (24, 8))
            conf.addKey('spectrogram.maxfreq', 12000,
                        doc="Highest frequency in a spectrogram")

        # The same effect can be achieved by passing the default/validator/doc

        default = {
            "keyA": 10,
            "keyB": 0.5,
            "keyC": "blue
        }

        validator = {
            "keyB::range": (0, 1),
            "keyC::choices": ("blue", "red")
        }

        docs = {
            "keyA": "documentation of keyA"
            "keyC": "documentation of keyC"
        }

        cfg = ConfigDict("myproj.subproj",
                         default=default,
                         validator=validator,
                         docs=docs)
        # no need to call .load in this case

        # Using inheritance
        class MyConfig(ConfigDict):
            def __init__(self):
                super().__init__(name="myconfig", default=default, validator=validator,
                                 docs=docs)

        cfg = MyConfig()

    """

    _registry: dict[str, ConfigDict] = {}

    _helpwidth: int = 58
    _infowidth: int = 58
    _valuewidth: int = 36

    def __init__(self,
                 name: str,
                 default: dict[str, Any]=None,
                 validator: dict[str, Any] = None,
                 docs: dict[str, str] = None,
                 precallback:Callable[[ConfigDict, str, Any, Any], Any]=None,
                 persistent=False,
                 load=True,
                 fmt='yaml',
                 sortKeys=False,
                 description='',
                 strict=True,
                 advancedPrefix='.') -> None:

        self._name = ''
        self._base = ''
        self._persistent = persistent
        self._configPath = None
        self._callbacks = []
        self._loaded = False
        self.bypassCallbacks = False
        self.description = description

        if name:
            name = _normalizeName(name)
            if not _isValidName(name):
                raise ValueError(f"name {name} is invalid for a config")
            previous = self._registry.get(name)
            if previous:
                if persistent and previous.persistent:
                    raise ValueError(f"A persistent ConfigDict with the name {name} already exists!")
                elif default != previous.default:
                    logger.warning(f"ConfigDict: instance with name {name} already created"
                                   "with different defaults. It will be overwritten")
            self._registry[name] = self
            self._name = name
        else:
            assert not persistent, "A persistent dict needs a name"
            load = False

        self.fmt = fmt
        super().__init__(default=default,
                         validator=validator,
                         docs=docs,
                         callback=self._mycallback,
                         precallback=precallback,
                         autoload=False,
                         advancedPrefix=advancedPrefix)
        self.sortKeys = sortKeys

        if default is not None:
            self._updateWithDefault()
            if load:
                self.load()

    @property
    def name(self) -> Optional[str]:
        """
        The name of this ConfigDict. The name determines where it is saved
        """
        return self._name

    @property
    def persistent(self) -> bool:
        """Is this a persistent ConfigDict?"""
        return self._persistent

    @persistent.setter
    def persistent(self, value) -> None:
        """Make this dict persistent. There can only be one persistent dict per name"""
        if self._persistent == value:
            return
        self._persistent = value
        if value:
            if self._name in self._registry:
                raise ValueError(f"A persistent ConfigDict with the name {self._name} already exists")
            if not self._name:
                raise ValueError("A ConfigDict without namecannot be set to persistent")
            self._ensureWritable()
            self._registry[self._name] = self
        else:
            assert self._name in self._registry
            del self._registry[self._name]

    def _mycallback(self, key, value):
        """
        own callback used to dispatch to any registered callbacks and save
        self after any change
        """
        if self.bypassCallbacks:
            return
        for pattern, func in self._callbacks:
            if re.match(pattern, key):
                func(self, key, value)
        if self._persistent:
            self.save()

    def update(self, d: dict=None, **kws) -> None:
        """
        Update this dict with the values in d.

        .. note::

            keywords have priority over d (similar to builtin dict)

        Args:
            d: values in this dictionary will overwrite values in self.
                Keys not present in self will raise an exception
            **kws: any key:value here will also be used to update self

        """
        if not d and not kws:
            return
        self._persistent, persistent = False, self._persistent
        CheckedDict.update(self, d, **kws)
        self._persistent = persistent
        if persistent:
            self.save()

    def copy(self: T) -> T:
        """
        Create a copy if this dict.

        The copy will be unnamed and not persistent. Use :meth:`ConfigDict.clone`
        to create a named/persistent clone of this dict.

        Returns:
            the copy of this dict
        """
        return self.clone()

    def isCongruentWith(self, other: ConfigDict) -> bool:
        """
        Returns True if self and other share same default
        """
        return self.default == other.default
        
    def clone(self: T, updates: dict = None, name: str = None, persistent=False, 
              cloneCallbacks=True, **kws
              ) -> T:
        """
        Create a clone of this dict

        Args:
            name: the name of the clone. If not given, the name of this dict is used. 
            persistent: Should the clone be made persitent? 
            cloneCallbacks: should the registered callbacks of the original (if any) be
                cloned?
            updates: a dict with updates
            **kws: same as updates but only for keys which are valid keywords

        Returns:
            the cloned dict
        """
        if name is None:
            name = self._name
        out = self.__class__(default=self.default, validator=self._validator, docs=self._docs,
                             persistent=persistent, load=False, name=name)
        out._bypass = True
        dict.update(out, self)
        out._bypass = False
        if updates:
            out.update(updates)
        if kws:
            out.update(**kws)
        if cloneCallbacks and self._callbacks:
            for pattern, func in self._callbacks:
                out.registerCallback(func, pattern)
        return out

    def registerCallback(self, func:Callable[[ConfigDict, str, Any], None], pattern:str=r".*") -> None:
        """
        Register a callback to be fired when a key matching the given pattern is changed.

        If no pattern is given, the function will be called for every key.

        Args:
            func: a function of the form ``(dict, key, value) -> None``, where *dict* is
                this ConfigDict itself, *key* is the key which was just changed and *value*
                is the new value.
            pattern: a regex pattern. The function will be called if the pattern matches
                the key being modified.

        """
        self._callbacks.append((pattern, func))

    def _ensureWritable(self) -> None:
        """ Make sure that we can serialize this dict to disk """
        folder, _ = os.path.split(self.getPath())
        if not os.path.exists(folder):
            os.makedirs(folder)

    def reset(self, save=True) -> None:
        """ Reset this dict to its default """
        super().reset()
        if save:
            self.save()

    def resetKey(self, key: str) -> None:
        """Reset the given key to its default value"""
        self[key] = self.default[key]

    def save(self, path: str = None, header:str='') -> None:
        """
        Save this to its persistent path (or a custom path)

        If this config was created with the `persistent` flag on,
        it does not need to be saved manually, it is saved whenever it
        is modified. However if it was created with ``persistent=False``
        then this method can be used to write this dict so it will be
        loaded in a future session

        Args:
            path: the path to save the config. If None and this
                is a named config, it is saved to the path returned by
                :meth:`~ConfigDict.getPath`
            header: if given, this string is written prior to the dict, as
                a comment. This is only supported when saving to yaml
        """
        if not path:
            path = self.getPath()
            fmt = self.fmt
        else:
            fmt = os.path.splitext(path)[1][1:]
            assert fmt in {'json', 'yaml', 'csv'}, f"Invalid format {fmt}, expected one of 'yaml', 'json', 'csv'"
        logger.debug(f"Saving config to {path}")
        if fmt is None:
            fmt = self.fmt
        if fmt == 'json':
            with open(path, "w") as f:
                json.dump(self, f, indent=True, sort_keys=True)
        elif fmt == 'yaml' or fmt == 'yml':
            self._saveAsYaml(path, header=header, sortKeys=self.sortKeys)
        elif fmt == 'csv':
            csvstr = self.asCsv()
            open(path, "w").write(csvstr)
        else:
            raise ValueError(f"Extention '{fmt}' not suported. It should be one of .yaml, .yml, .json, .csv")
        assert os.path.exists(path), f"Saved file to '{path}', but file does not exist"

    def dump(self):
        """ Dump this config to stdout """
        print(str(self))

    def _asRows(self):
        rows = []
        for key, value in self.items():
            infostr = self._infoStr(key)
            doc = self.getDoc(key)
            rows.append((key, str(value), infostr, doc if doc else ""))
        return rows

    def generateRstDocumentation(self, maxWidth=80, withName=True, withDescription=True,
                                 withLink=True, linkPrefix=''
                                 ) -> str:
        """
        Generate ReST documentation for this dictionary

        The generated string can then be dumped to a file and included
        in documentation

        Args:
            maxWidth: the max. width of a line
            withName: if True, add the name of the config (if it has a name)
            withDescription: if True, add this dict's description (if it has any)
            withLink: if True, for each key:value pair generate a RST link using the given linkPrefix
                For example, for a key 'foo' and a linkPrefix='config' the generated link will be
                ``.. _configfoo``. This link can be used within the documentation to link to this key

        Returns:
            the generated rst documentation, as str.
        """
        lines = []
        _ = lines.append

        if withName and self.name:
            _(self.name)
            _("-" * len(self.name))
            _('')
        if withDescription and self.description:
            _(textwrap.wrap(self.description, width=maxWidth))
            _('\n------------------------\n')
        for key, value in self.default.items():
            if withLink:
                linkkey = _asRstLinkKey(key)
                if linkPrefix:
                    linkkey = linkPrefix + linkkey
                _(f".. _{linkkey}:\n")
            _(f"{key}:")
            if isinstance(value, str) and not value:
                value = "''"
            _(f"    | Default: **{value}**  -- ``{self.getTypestr(key)}``")
            if choices := self.getChoices(key):
                choices = sortNatural([str(_) for _ in choices])
                choicestr = ', '.join(choices)
                _(f"    | Choices: ``{choicestr}``")
            if valuerange := self.getRange(key):
                a, b = valuerange
                _(f"    | Between {a} - {b}")
            if doc := self.getDoc(key):
                _(f"    | *{doc}*")
            _("")
        return "\n".join(lines)

    def asCsv(self) -> str:
        """
        Returns this dict as a csv str, with columns: key, value, spec, doc
        """
        rows = [("# key", "value", "spec", "doc")]
        rows.extend(self._asRows())
        from io import StringIO
        import csv
        s = StringIO()
        writer = csv.writer(s)
        writer.writerows(rows)
        return s.getvalue()

    def _infoStr(self, k: str) -> str:
        info = []
        choices = self.getChoices(k)
        if choices:
            choices = sortNatural([str(choice) for choice in choices])
            choicestr = "{" + ", ".join(str(ch) for ch in choices) + "}"
            info.append(choicestr)
        elif (keyrange := self.getRange(k)) is not None:
            low, high = keyrange
            info.append(f"between {low} - {high}")
        else:
            typestr = self.getTypestr(k)
            info.append("type: " + typestr)

        if self[k] != self.default[k]:
            info.append(f'default: {self.default[k]}')
        return" | ".join(info) if info else ""

    def _repr_html_(self) -> str:
        parts = [f'<div><h4>{type(self).__name__}: <strong>{self.name}</strong></h4>']
        if self.persistent:
            parts.append(f'persistent (<code>"{self.getPath()}"</code>)')
        parts.append("<br>")
        rows = []
        keys = self._sortedKeys()
        for k in keys:
            v = self[k]
            descr = self.getDoc(k)
            if v == self.default[k]:
                strv = str(v)
            else:
                strv = f'<i><b>{v}</b></i>'
            rows.append((k, strv, self._infoStr(k), descr))
        table = _htmlTable(rows, headers=('Key', 'Value', 'Type', 'Descr'), maxwidths=[0, 0, 150, 400],
                           rowstyles=('strong', 'code', None, None))
        parts.append(table)
        parts.append("</div>")
        return "".join(parts)

    def _repr_pretty_(self, printer, cycle) -> str:
        return printer.text(str(self))

    def _repr_rows(self) -> list[str]:
        try:
            termwidth = os.get_terminal_size()[0] - 6
        except OSError:
            termwidth = 80
        maxwidth = self._infowidth + self._valuewidth + max(len(k) for k in self.keys())
        infowidth = int(self._infowidth / maxwidth * termwidth)
        valuewidth = int(self._valuewidth / maxwidth * termwidth)
        rows = []
        keys = sorted(self.keys())
        for k in keys:
            v = self[k]
            infostr = self._infoStr(k)
            if len(infostr) > infowidth:
                infolines = textwrap.wrap(infostr, infowidth)
                infostr = "\n".join(infolines)
            valuestr = str(v)
            if len(valuestr) > valuewidth:
                valuestr = "\n".join(textwrap.wrap(valuestr, valuewidth))
            rows.append((k, valuestr, infostr))
            doc = self.getDoc(k)
            if doc:
                if len(doc) > infowidth:
                    doclines = textwrap.wrap(doc, infowidth)
                    doc = "\n".join(doclines)
                rows.append(("", "", doc))
        return rows

    def __str__(self) -> str:
        import tabulate
        header = f"Config: {self._name}\n"
        rows = self._repr_rows()
        return header + tabulate.tabulate(rows) + '\n'

    def getPath(self) -> str:
        """ Return the path this dict will be saved to

        If the dict has no name, an empty string is returned
        """
        if not self._name:
            return ''
        if not self._configPath:
            self._configPath = configPathFromName(self._name, self.fmt)
        return self._configPath

    def edit(self, waitOnModified=True, sortKeys=False) -> None:
        """
        Edit this config by opening it in an external application.

        The format used is *yaml*. This is independent of the format used for
        persistence. The application used is the user's default application
        for the .yaml format and can be configured at the os level. In macos
        we use ``open``, in linux ``xdg-open`` and in windows ``start``, which
        all respond to the user's own configuration regarding default applications.

        .. note::

            A temporary file is created for editing. The persisted file is only
            modified if the editing is accepted.

        Args:
            waitOnModified: if True, the transaction is accepted whenever the
                file being edited is saved. Otherwise a message box is created
                which needs to be clicked in order to confirm the transaction.
                Just exiting the application will not cancel the edit job since
                many applications which have a server mode or unique instance
                mode might in fact exit right away from the perspective of the
                subprocess which launched them
            sortKeys: if True, keys appear in sorted order
        """
        header = _editHeaderWatch if waitOnModified else _editHeaderPopup
        configfile = tempfile.mktemp(suffix=".yaml")
        self._saveAsYaml(configfile, header=header, sortKeys=sortKeys)
        assert os.path.exists(configfile)
        _openInEditor(configfile)
        if waitOnModified:
            try:
                _notify(f"Config Edit: {self.name}", "Modify the values as needed. Save the file to accept the changes "
                        f"or press ctrl-c at the python prompt to cancel (path: {configfile})")
                _waitOnFileModified(configfile)
                _notify("Edit", "Editing finished, any further modifications will have no effect")
            except KeyboardInterrupt:
                logger.debug("Editing aborted")
                _notify(f"Config Edit: {self.name}", "Editing aborted")
                return
        else:
            _waitForClick(title=self.name)
        self.load(configfile)
        if self.persistent:
            self.save()
            
    def _updateWithDefault(self, bypass=True) -> None:
        try:
            self._bypass = True
            dict.update(self, self.default)
            self._bypass = False
        except ValueError as e:
            errmsg = textwrap.indent(str(e), prefix="    ")
            raise ValueError(f"Could not load default dict, error:\n{errmsg}")

    def _fill(self, other: dict) -> None:

        for key in other:
            if key not in self:
                self[key] = other[key]

    def load(self, configpath: str = None) -> None:
        """
        Read the saved config, update self.

        If there is no saved version or the dict has no name, then
        the dict is set to the default defined at construction.

        When defining the default iteratively (via addKey), calling
        load marks the end of the definition: after calling load no other
        keys can be added to this dict.

        Args:
            configpath: an custom path to load a saved version from. Otherwise
                it is loaded from :meth:`ConfigDict.getPath` (this is only
                possible if the dict has a name, since the resolved path
                is determined from the name)

        Example
        -------

        .. code::

            from configdict import ConfigDict
            conf = ConfigDict('foo.bar')
            conf.addKey('key1', 'value1', ...)
            conf.addKey('key2', 'value2', ...)
            ...
            # When finished defining keys, call .load
            conf.load()

            # Now the dict can be used

        When
        """
        assert self.default
        if len(self) == 0:
            # load after defining the default
            super().update(self.default)
        if configpath is None:
            configpath = self.getPath()
        if not configpath or not os.path.exists(configpath):
            logger.debug(f"No saved version found for dict '{self.name}', using default")
            super().update(self.default)
            return
        logger.debug(f"Reading config from disk: {configpath}")
        confdict = _loadDict(configpath)
        if confdict is None:
            logger.error("Could not load saved config, skipping")
            return

        # only keys in default should be accepted, but keys in the read
        # config should be discarded with a warning
        keysNotInDefault = confdict.keys() - self.default.keys()
        needsSave = False
        if keysNotInDefault:
            logger.warning(f"ConfigDict {self._name}, saved at {configpath}\n"
                           "There are keys defined in the saved config which are not"
                           " present in the default config, they will be skipped: \n"
                           f"   {keysNotInDefault}")
            for k in keysNotInDefault:
                del confdict[k]
            needsSave = True

        # merge strategy:
        # * if a key is shared between default and read dict, read dict has priority
        # * if a key is present only in default, it is added

        # check invalid values
        if self._validator:
            keysWithInvalidValues = []
            for k, v in confdict.items():
                errormsg = self.checkValue(k, v)
                if errormsg:
                    logger.error(f"Error while loading config {self.name} (path: {configpath})")
                    logger.error(errormsg)
                    logger.error(f"    Using default: {self.default[k]}")
                    keysWithInvalidValues.append(k)
            for k in keysWithInvalidValues:
                del confdict[k]
        super().update(confdict)
        self._loaded = True
        if needsSave and self.persistent:
            self.save()


def _makeName(configname: str, base: str = None) -> str:
    if base is not None:
        return f"{base}.{configname}"
    else:
        return f".{configname}"


def _mergeDicts(readdict: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    """
    Merge readdict into default
    Args:
        readdict:
        default:

    Returns:
        the merged dict
    """
    out = {}
    sharedkeys = readdict.keys() & default.keys()
    for key in sharedkeys:
        out[key] = readdict[key]
    onlyInDefault = default.keys() - readdict.keys()
    for key in onlyInDefault:
        out[key] = default[key]
    return out


def _parseName(name: str) -> tuple[str, Optional[str]]:
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
    """
    check if name is a valid name for a config
    """
    if not _isValidName(name):
        raise ValueError(
            f"{name} is not a valid name for a config."
            " It should contain letters, numbers and any of '.', '_', ':'")


def getConfig(name: str) -> Optional[ConfigDict]:
    """
    Retrieve a previously created ConfigDict.

    This will NOT load a saved config since for a ConfigDict to be properly
    defined a default config must accompany the saved version. In order to
    load a saved config as default just load it as a normal .yaml or .json
    file and use that dict as the default.

    Args:
        name: the unique id of the configuration, as passed to ConfigDict

    Returns:
        the ConfigDict, if found. None otherwise.

    """
    assert name, "name is empty"
    name = _normalizeName(name)
    _checkName(name)
    return ConfigDict._registry.get(name)


def activeConfigs() -> dict[str, ConfigDict]:
    """
    Returns a dict of active configs
    """
    return ConfigDict._registry.copy()


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


def configPathFromName(name: str, fmt='yaml') -> str:
    """
    Given a config name, return the path where it should be saved

    Args:
        name: the name of this config, with the format [prefix.]name
        fmt: the format of the config (valid options: json, yaml)

    Returns:
        the path corresponding to this config name

    """
    name = _normalizeName(name)
    userconfigdir = appdirs.user_config_dir()
    base, configname = _parseName(name)
    if fmt == 'json':
        configfile = configname + ".json"
    elif fmt == 'yaml':
        configfile = configname + '.yaml'
    else:
        raise ValueError("Formats supported: json, yaml")
    if base is not None:
        configdir = os.path.join(userconfigdir, base)
    else:
        configdir = userconfigdir
    return os.path.join(configdir, configfile)
