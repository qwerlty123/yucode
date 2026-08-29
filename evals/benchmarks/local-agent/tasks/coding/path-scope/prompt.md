Fix `path_scope.py`. Resolve a user path under an allowed root. Reject absolute paths, lexical traversal, and symlink escapes; return a resolved `Path` for valid nested paths. Do not create files.
