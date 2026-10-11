5.0.0 / 2024-09-10
=========================

* Breaking changes:
  * `res.status()` accepts only integers, and input must be greater than 99 and less than 1000
  * `res.redirect('back')` and `res.location('back')` is no longer a supported magic string, explicitly use `req.get('Referrer') || '/'`.
* change:
  * `res.clearCookie` will ignore user provided `maxAge` and `expires` options
* deps: cookie-signature@^1.2.1
* deps: debug@4.3.6

4.21.0 / 2024-09-11
==========

  * Deprecate `res.location("back")` and `res.redirect("back")` magic string
  * deps: serve-static@1.16.2
