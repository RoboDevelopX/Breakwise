2.1.0 (2023-11-13)
==================

Removals
--------

- Removed support for the deprecated urllib3[secure] extra. (`#2680 <https://github.com/urllib3/urllib3/issues/2680>`__)
- Removed support for the end-of-life Python 3.7. (`#3143 <https://github.com/urllib3/urllib3/issues/3143>`__)

Bugfixes
--------

- Fixed an issue where ``HTTPResponse.read()`` would not respect ``amt``. (`#3128 <https://github.com/urllib3/urllib3/issues/3128>`__)


2.0.0 (2023-04-26)
==================

Removed
-------

* Removed deprecated ``Retry`` options ``method_whitelist``, ``DEFAULT_REDIRECT_HEADERS_BLACKLIST`` (`#2086 <https://github.com/urllib3/urllib3/issues/2086>`__).
* Removed ``urllib3.HTTPResponse.from_httplib`` (`#2648 <https://github.com/urllib3/urllib3/issues/2648>`__).

Deprecated
----------

* Deprecated ``HTTPResponse.getheaders()`` and ``HTTPResponse.getheader()`` methods.


1.26.0 (2020-11-10)
===================

* Deprecated ``Retry`` options ``Retry.DEFAULT_METHOD_WHITELIST``, ``Retry.DEFAULT_REDIRECT_HEADERS_BLACKLIST``
  and ``Retry(method_whitelist=...)`` in favor of ``Retry.DEFAULT_ALLOWED_METHODS``,
  ``Retry.DEFAULT_REMOVE_HEADERS_ON_REDIRECT``, and ``Retry(allowed_methods=...)``
  (Pull #2000) **Starting in urllib3 v2.0: Deprecated options will be removed**
