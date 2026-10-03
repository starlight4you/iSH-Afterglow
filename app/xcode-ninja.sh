#!/bin/sh

# Try to figure out the user's PATH to pick up their installed utilities.
# (no sudo: it would hang the Xcode build waiting for a password)
for dir in /opt/homebrew/bin /usr/local/bin /opt/local/bin; do
    case ":$PATH:" in *":$dir:"*) ;; *) PATH="$PATH:$dir";; esac
done
export PATH

ninja "$@"
