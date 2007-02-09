"""
db schema for infogami core.
"""

import web
from utils import dbsetup

upgrade = dbsetup.module("core").upgrade

schema = """
CREATE TABLE site (
  id serial primary key,
  url text
)

----

CREATE TABLE page (
  id serial primary key,
  site_id int references site,
  path text
)

----

CREATE TABLE version (
  id serial primary key,
  page_id int references page,
  author text,
  created timestamp default (current_timestamp at time zone 'utc')
)

----

CREATE TABLE datum (
  id serial primary key,
  version_id int references version,
  key text,
  value text
)
"""

@upgrade
def setup():
    for table in schema.split('----'):
        web.query(table)

@upgrade
def add_login_table():
    """add login table"""
    web.query("""
        CREATE TABLE login (
          id serial primary key,
          name text unique,
          email text,
          password text
        )""")

def initialize_revisions():
    pages = web.query("SELECT * FROM page")

    for p in pages:
        page_id = p.id
        versions = web.query("SELECT * FROM version WHERE page_id=$page_id", vars=locals())
        for i, v in enumerate(versions):
            id = v.id
            web.update('version', where='id=$id', revision=i+1, vars=locals())

@upgrade
def add_version_revision():
    """revision column is added to version table."""
    web.query("ALTER TABLE version ADD COLUMN revision int default 0")
    initialize_revisions()

