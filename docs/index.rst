.. configdict documentation master file, created by
   sphinx-quickstart on Tue Jan 19 13:59:39 2021.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

configdict
==========

This package provides two classes, :class:`~configdict.configdict.CheckedDict` and :class:`~configdict.configdict.ConfigDict`, 
which allow to define a dict with default values and a set of allowed keys.
Any modification to the dict can be validated against a set of rules.


.. toctree::
   :maxdepth: 3

      
.. automodapi:: configdict.configdict
     :inherited-members:
