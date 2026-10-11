# Changelog

## v1.19.0 — July 22, 2026

This release raises the form-data security floor.

## 🔒 Security Fixes

- Multipart Form Data: Raised the form-data dependency floor to ^4.0.6, preventing CRLF injection GHSA-hmw2-7cc7-3qxx. (#11028)

## 🐛 Bug Fixes

- Proxy Bypass: Canonicalized IPv4 shorthand during NO_PROXY matching. (#11029)

## v1.18.0 — June 1, 2026

## 🚀 New Features

- Added the `fetchOptions` config. (#10900)
