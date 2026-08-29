Fix `query.mjs`. Parse a query string into a null-prototype object whose values are arrays. Preserve duplicate order and blank values, decode `+` as space, and reject dangerous prototype keys.
