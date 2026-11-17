-- Feature catalog: the flags and subcommands the installed `claude` lists in its `--help`, one
-- set of rows per Claude Code version. Nothing here is hardcoded; it is parsed from the binary.
--   kind 'flag'            a top-level option (parent is NULL)
--   kind 'subcommand'      a command listed under "Commands:" (parent is NULL)
--   kind 'subcommand_flag' an option of that subcommand (parent is the subcommand's name)

CREATE TABLE catalog (
    version     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('flag', 'subcommand', 'subcommand_flag')),
    name        TEXT NOT NULL,
    short       TEXT,
    parent      TEXT,
    description TEXT NOT NULL,
    PRIMARY KEY (version, kind, name, parent)
);

-- SQLite treats NULLs in a primary key as distinct, so the key alone would not stop a repeated
-- top-level row. This index does.
CREATE UNIQUE INDEX catalog_unique ON catalog (version, kind, name, COALESCE(parent, ''));
