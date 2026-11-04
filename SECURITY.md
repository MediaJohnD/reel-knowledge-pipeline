# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/MediaJohnD/reel-knowledge-pipeline/security/advisories/new)
rather than a public issue. Include the affected version or commit, steps to
reproduce, and the impact you expect. Expect an initial reply within a week.

## Scope

In scope: the URL-validation and private-address guards, the webhook
(authentication and binding), credential handling, and anything that lets a
submitted URL reach a service on the host or its network.

Out of scope: findings that need the owner's own logged-in Instagram profile or
cookies to be compromised first, and platforms the project documents as
unsupported.

## Supported versions

Only the latest commit on `master` is supported.
