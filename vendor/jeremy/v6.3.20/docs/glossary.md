# Glossary

## Bootstrap Script

A pinned GitHub release asset that detects the operator host OS and CPU
architecture, selects the matching runtime payload zip, downloads it from the
same release tag, extracts it, and starts the payload installer.

## Runtime Payload Zip

A versioned release zip named
`pool-stack-docker-<tag>-linux-<arch>.zip`. It contains the normal installer
launchers, stack files, and native Linux service binaries for one Docker runtime
architecture.

## Linux ARM64 Runtime

The `linux-arm64` runtime payload used for native ARM64 Linux containers. Linux
ARM64, macOS ARM64 Docker Desktop, and Windows ARM64 Docker Desktop hosts select
this payload.
