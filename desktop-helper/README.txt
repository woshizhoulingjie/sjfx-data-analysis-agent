SJFX Windows desktop protection helper

sjfx-assistant.exe is the reviewed Go 1.27.2 Windows amd64 helper. It is a
CGO_DISABLED build; its native imports are Windows OS libraries only.
SOURCE.json records source, binary, exact Go module hashes and original notices.
An offline rebuild from these sources matched the delivered binary byte for byte.

The source directory contains the helper source, embedded assets and component
tests. offline-module-proxy preserves all eleven exact declared Go module source
archives and metadata; go.sum binds their content hashes. licenses preserves Go
runtime and dependency license/notice files verbatim. First-party source remains
subject to the SJFX project's terms.

BUILD.ps1 rebuilds with a separately supplied Go 1.27.2 Windows amd64 SDK and a
new disposable build directory, using only the bundled offline module proxy.
The SDK is a build prerequisite; executing the helper needs no Go installation.

The application supervisor supplies a new protected instance state directory,
an on-disk private credential and an independent IPv4 loopback port. Runtime
identity files, credentials, tokens and prior helper databases are not included.
Native access-audit coverage requires the Windows privileges described in the
runtime contract. Ordinary health/authentication does not claim audit coverage.
