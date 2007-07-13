import web
import logger
import datetime
from lru import LRU

class TDBException(Exception): pass

class NotFound(TDBException): pass
class BadData(TDBException): pass
class SecurityError(TDBException): pass

class Thing:
    @staticmethod
    def _reserved(attr):
        return attr.startswith('_') or attr in [
          'id', 'parent', 'name', 'type', 'latest_revision', 'v', 'h', 'd', 'latest', 'versions', 'save', '_tdb']
    
    def __init__(self, tdb, id, name, parent, latest_revision, v, type, d):
        self._tdb = tdb
        self.id, self.name, self.parent, self.type, self.d, self.v, self.latest_revision = \
            id and int(id), name, parent, type, d, v, latest_revision
            
        if self.id:
            self.h = History(tdb, self.id)
        else:
            self.h = None
        self._dirty = False
        self.d = web.storage(self.d)

    def copy(self):
        # there could be a problem in sharing lists values in self.d
        return Thing(self._tdb, self.id, self.name, self.parent, self.latest_revision, self.v, self.type, self.d)
                    
    def __repr__(self):
        dirty = (self._dirty and " dirty") or ""
        return '<Thing "%s" at %s%s>' % (self.name, self.id, dirty)

    def __str__(self): return self.name
    
    def __cmp__(self, other):
        return cmp(self.id, other.id)

    def __eq__(self, other):
        return self.id == other.id and self.name == other.name and dict(self.d) == dict(other.d)

    def __ne__(self, other):
        return not (self == other)
    
    def __getattr__(self, attr):
        if not Thing._reserved(attr) and self.d.has_key(attr):
            return self.d[attr]
        raise AttributeError, attr
        
    def __getitem__(self, attr):
        if not Thing._reserved(attr) and self.d.has_key(attr):
            return self.d[attr]
        raise KeyError, attr

    def get(self, key, default=None):
        return getattr(self, key, default)

    def c(self, name):
        return self._tdb.withName(name, self)

    def __setattr__(self, attr, value):
        if Thing._reserved(attr):
            self.__dict__[attr] = value
            if attr == 'type':
                self._dirty = True
        else:
            self.d[attr] = value
            self._dirty = True
            
    __setitem__ = __setattr__
    
    def setdata(self, d):
        self.d = d
        self._dirty = True

    def save(self, comment='', author=None, ip=None):
        if self._dirty:
            self._tdb.save(self, author, comment, ip)
            self._dirty = False

    def __hash__(self):
        return self.id
            
class Version:
    def __init__(self, tdb, id, thing_id, revision, author_id, ip, comment, created):
        web.autoassign(self, locals())
        self.thing = tdb.withID(thing_id, revision, lazy=True)
        self.author = (author_id and tdb.withID(author_id, lazy=True)) or None
        
    def __cmp__(self, other):
        return cmp(self.id, other.id)
        
    def __repr__(self): 
        return '<Version %s@%s at %s>' % (self.thing.id, self.revision, self.id)

# define any and all for python2.4
try:
    any
except:
    def any(seq):
        for x in seq:
            if x:
                return True
        return False

    def all(seq):
        for x in seq:
            if not x: 
                return False
        return True

def match_query(query, data):
    """Tests whether all key-value pairs in query are present in data.
        >>> match_query(dict(a=1, b=2), dict(a=1, b=2, c=3))
        True
        >>> match_query(dict(a=1, b=2), dict(a=1, b=4, c=3))
        False
        >>> match_query(dict(a=1, b=2), dict(a=1, c=3))
        False
        >>> match_query(dict(a=1, b=2), dict(a=1, b=[2, 4], c=3))
        True
    """
    def match(a, b):
        if isinstance(b, list):
            return any(a == bb for bb in b)
        else:
            return a == b
    
    nothing = object()        
    return all(match(query[k], data.get(k, nothing)) for k in query)

class Things:
    def __init__(self, tdb, limit=None, **query):
        self.tdb = tdb
        self.query = query
        tables = ['thing', 'version']            
        what = "thing.id"
        where = "thing.id = version.thing_id AND thing.latest_revision = version.revision"
        
        if 'parent' in query:
            parent = query.pop('parent')
            where += web.reparam(' AND thing.parent_id = $parent.id', locals())
        
        if 'type' in query:
            type = query.pop('type')
            query['__type__'] = type.id
        
        n = 0
        for k, v in query.items():
            n += 1
            if isinstance(v, Thing):
                v = v.id
            tables.append('datum AS d%s' % n)
            where += ' AND d%s.version_id = version.id AND ' % n 
            # using substr to use index. 
            #@@ when len(value) > 250, this won't work.
            where += web.reparam('d%s.key = $k AND substr(d%s.value, 0, 250) = $v' % (n, n), locals())
        
        result = web.select(tables, what=what, where=where, limit=limit)
        self.values = tdb.withIDs([r.id for r in result])

    def matches(self, thing):
        """Tests whether `thing` matches this query."""
        return match_query(self.query, dict(thing.d, parent=thing.parent, type=thing.type))
        
    def __iter__(self):
        return iter(self.values)
        
    def list(self):
        return self.values

class Versions:
    def __init__(self, tdb, limit=None, **query):
        self.query = query
        self.versions = None
        self.limit = limit
        self.tdb = tdb
        self.init()
    
    def init(self):
        tables = ['thing', 'version']
        what = 'version.*, thing.name, thing.parent_id, thing.latest_revision'
        where = "thing.id = version.thing_id"
        
        if 'parent' in self.query:
            parent = self.query.pop('parent')
            self.query['parent_id'] = parent.id
            
        if 'author' in self.query:
            author = self.query.pop('author')
            self.query['author_id'] = author.id
            
        for k, v in self.query.items():
            where += web.reparam(' AND %s = $v' % (k,), locals())
                    
        self.tdb.stats.version_queries += 1

        def version(r):
            """Creates a version for result `r` and sets version.thing to 
            a partial thing with thing.* data from the result. 
            """
            name = r.pop('name')
            parent_id = r.pop('parent_id')
            latest_revision = r.pop('latest_revision')
            v = Version(self.tdb, **r)
            t = LazyThing(
                    lambda: self.tdb.withID(id=r.thing_id, revision=r.revision), 
                    id=r.thing_id, name=name, parent_id=parent_id, latest_revision=latest_revision, v=v)
            v.thing = t
            return v

        result = web.select(tables, what=what, where=where, order='id desc', limit=self.limit)
        self.versions = [version(r) for r in result]
    
    def matches(self, thing):
        """Tests whether thing.v matches this query."""
        return match_query(self.query, dict(thing.v.__dict__, parent=thing.parent, parent_id=thing.parent.id, thing_id=thing.id))
        
    def __getitem__(self, index):
        return self.versions[index]
        
    def __eq__(self, other):
        return self.versions == other.versions
    
    def __len__(self):
        return len(self.versions)
        
    def __str__(self):
        return str(self.versions)
        
def History(tdb, thing_id):
    return tdb.Versions(thing_id=thing_id)
        
class Proxy:
    def __init__(self, _o):
        self.__dict__['_o'] = _o
        
    def __getattr__(self, key):
        return getattr(self._get(), key)

    def __setattr__(self, key, value):
        return getattr(self._get(), key, value)
    
    def _get(self):
        return self._o
            
class LazyProxy(Proxy):
    def __init__(self, constructor):
        self.__dict__['_constructor'] = constructor
        Proxy.__init__(self, None)
        
    def _get(self):
        if self._o is None:
            self.__dict__['_o'] = self._constructor()
        return self._o
        
    def __hash__(self):
        raise TypeError, "Not hashable."
    
class LazyThing(Thing):
    def __init__(self, constructor, **fields):
        self.__dict__['_constructor'] = constructor
        self.__dict__['_thing'] = None
        self.__dict__.update(fields)
    
    def __getattr__(self, key):
        return getattr(self._get(), key)
        
    def __setattr__(self, key, value):
        return getattr(self._get(), key, value)
        
    def __nonzero__(self):
        return True
    
    def _get(self):
        if self._thing is None:
            self.__dict__['_thing'] = self._constructor()
        return self._thing

    def __hash__(self):
        return self.id
        
class SimpleTDBImpl:
    """Simple TDB implementation without any optimizations."""
    
    def __init__(self):
        self.stats = web.storage(queries=0, version_queries=0, saves=0)
        self.root = self.withID(1, lazy=True)
        self.parent = self
        self.hints = web.storage()
            
    #@@ Hack    
    def set_parent(self, parent):
        self.parent = parent
        
    def setup(self):
        try:
            self.withID(1)
        except NotFound:
            # create root of all types
            self.new("root", self.root, self.root).save()

    def new(self, name, parent, type, d=None):
        """Creates a new thing."""
        if d == None: d = {}
        t = Thing(self.parent, None, name, parent, latest_revision=None, v=None, type=type, d=d)
        t._dirty = True
        return t
        
    def withID(self, id, revision=None, lazy=False):
        """queries for thing with the specified id.
        If revision is not None, thing at that revision is returned.
        """
        if lazy:
            return LazyThing(lambda: self.withID(id, revision, lazy=False), id=id)
        else:
            t = self._with(id=int(id))
            return self._load(t, revision)

    def withName(self, name, parent, revision=None, lazy=False):
        if lazy:
            return LazyThing(lambda: self.withName(name, parent, revision, lazy=False), name=name, parent=parent)
        else:
            t = self._with(name=name, parent_id=int(parent.id))
            return self._load(t, revision)
                
    def _with(self, **kw):
        self.stats.queries += 1
        try:
            wheres = []
            for k, v in kw.items():
                w = web.reparam(k + " = $v", locals())
                wheres.append(str(w))
            where = " AND ".join(wheres)
            return web.select('thing', where=where)[0]
        except IndexError:
            raise NotFound
        
            
    def _load(self, t, revision=None):
        id, name, parent, latest_revision = t.id, t.name, self.withID(t.parent_id, lazy=True), t.latest_revision
        revision = revision or latest_revision
        
        v = web.select('version',
            where='version.thing_id = $id AND version.revision = $revision',
            vars=locals())[0]
        v = Version(self.parent, **v)
        data = web.select('datum',
                where="version_id = $v.id",
                order="key ASC, ordering ASC",
                vars=locals())

        d, type = self._parse_data(data)
        parent = self.withID(t.parent_id, lazy=True)
        t = Thing(self.parent, t.id, t.name, parent, latest_revision, v, type, d)
        v.thing = t
        return t

    def _parse_data(self, data):
        d = {}
        for r in data:
            value = r.value
            if r.data_type == 0:
                pass # already a string
            elif r.data_type == 1:
                value = self.withID(int(value), lazy=True)
            elif r.data_type == 2:
                value = int(value)
            elif r.data_type == 3:
                value = float(value)

            if r.ordering is not None:
                d.setdefault(r.key, []).append(value)
            else:
                d[r.key] = value

        type = d.pop('__type__')
        return d, type
        
    def withIDs(self, ids, lazy=False):
        """Return things for the specified ids."""
        return [self.withID(id, lazy) for id in ids]
        
    def withNames(self, names, parent, lazy=False):
        return [self.withName(name, parent) for name in names]
            
    @staticmethod
    def savedatum(vid, key, value, ordering=None):
        # since only one level lists are supported, 
        # list type can not have ordering specified.
        if isinstance(value, list) and ordering is None:
            for n, item in enumerate(value):
                SimpleTDBImpl.savedatum(vid, key, item, n)
            return
        elif isinstance(value, str):
            dt = 0
        elif isinstance(value, Thing):
            dt = 1
            value = value.id
        elif isinstance(value, (int, long)):
            dt = 2
        elif isinstance(value, float):
            dt = 3
        else:
            raise BadData, value
        web.insert('datum', False, 
          version_id=vid, key=key, value=value, data_type=dt, ordering=ordering)        

    def save(self, thing, author=None, comment='', ip=None):
        """Saves thing. author, comment and ip are stored in the version info."""
        self.stats.saves += 1

        _run_hooks("before_new_version", thing)
        web.transact()
        if thing.id is None:
            thing.id = web.insert('thing', name=thing.name, parent_id=thing.parent.id, latest_revision=1)
            revision = 1
            tid = thing.id

            #@@ this should be generalized
            if thing.name == 'type/type':
                thing.type = thing
        else:
            tid = thing.id
            result = web.query("SELECT revision FROM version \
                WHERE thing_id=$tid ORDER BY revision DESC LIMIT 1 \
                FOR UPDATE NOWAIT", vars=locals())
            revision = result[0].revision+1
            web.update('thing', where='id=$tid', latest_revision=revision, vars=locals())

        author_id = author and author.id
        vid = web.insert('version', thing_id=tid, comment=comment, 
            author_id=author_id, ip=ip, revision=revision)

        for k, v in thing.d.items():
            SimpleTDBImpl.savedatum(vid, k, v)
        SimpleTDBImpl.savedatum(vid, '__type__', thing.type)

        logger.transact()
        try:
            if revision == 1:
                logger.log('thing', tid, name=thing.name, parent_id=thing.parent.id)
            logger.log('version', vid, thing_id=tid, author_id=author_id, ip=ip, 
                comment=comment, revision=revision)           
            logger.log('data', vid, __type__=thing.type, **thing.d)
            web.commit()
        except:
            logger.rollback()
            raise
        else:
            logger.commit()
        thing.id = tid
        #@@ created should really be the datetime from database, but this saves a query.
        thing.v = Version(self.parent, vid, thing.id, revision, author_id, ip, comment, created=datetime.datetime.now())
        thing.h = History(self.parent, thing.id)
        thing.latest_revision = revision
        thing._dirty = False
        _run_hooks("on_new_version", thing)
    
    def Things(self, limit=None, **query):
        return Things(self, limit=limit, **query)

    def Versions(self, limit=None, lazy=True, **query):
        if lazy:
            return LazyProxy(lambda: self.Versions(limit=limit, lazy=False, **query))
        else:
            return Versions(self, limit=limit, **query)
    
    def stats(self):
        """Returns statistics about performance as a dictionary.
        """
        return self.stats

class BetterTDBImpl(SimpleTDBImpl):
    """A faster tdb implementation."""
    def withIDs(self, ids, lazy=False):
        try:
            things = self._query(thing__id=ids)
            return self._reorder(things, lambda t: t.id, ids)
        except KeyError, k:
            raise NotFound, k
        else:
            self.stats.queries += 1
        
    def withNames(self, names, parent, lazy=False):
        try:
            things = self._query(name=names, parent_id=parent.id)
            return self._reorder(things, lambda t: t.name, names)
        except KeyError, k:
            raise NotFound, k
        else:
            self.stats.queries += 1
            
    def withID(self, id, revision=None, lazy=False):
        if lazy:
            return LazyThing(lambda: self.withID(id, revision, lazy=False), id=id)
        else:
            try:
                return self._query(thing__id=id, revision=revision)[0]
            except IndexError:
                raise NotFound, id
                
    def withName(self, name, parent, revision=None, lazy=False):
        if lazy:
            return LazyThing(lambda: self.withName(name, parent, revision, lazy=False), name=name, parent=parent)
        else:
            try:
                return self._query(name=name, parent_id=parent.id, revision=revision)[0]
            except IndexError:
                raise NotFound, name
                
    def _reorder(self, things, key, order):
        d = {}
        for t in things:
            d[key(t)] = t
        return [d[k] for k in order]
        
    def _query(self, revision=None, **kw):
        self.stats.queries += 1
        things = {}
        versions = {}
        datum = {}
        
        tables = ['thing', 'version', 'datum']
        
        # what = thing.*, version.* and datum.*
        whats = [
            'thing.id', 'thing.parent_id', 'thing.name', 'thing.latest_revision',
            'version.id as version_id', 'version.revision', 'version.author_id', 
            'version.ip', 'version.comment', 'version.created',
            'datum.key', "datum.value", 'datum.data_type', 'datum.ordering']
        what = ", ".join(whats)
        
        # join thing, version and datum tables
        where = "thing.id = version.thing_id"
        if revision is None:
            where += " AND thing.latest_revision = version.revision"
        else:
            where += web.reparam(" AND version.revision = $revision", locals())
        where += " AND version.id  = datum.version_id"
        
        # add additional wheres specified
        for k, v in kw.items():
            k = k.replace('__', '.')
            if isinstance(v, web.iters):
                where += " AND " + web.sqlors(k + " = ", v)
            else:
                where += " AND " + web.reparam(k + " = $v", locals())
                
        result = web.select(tables, what=what, where=where)

        # process the result row by row
        for r in result:
            if r.id not in things:
                vkeys = "version_id", "id", "revision", "author_id", "ip", "comment", "created"
                values = [r[k] for k in vkeys]
                versions[r.id] = Version(self, *values)

                things[r.id] = r.name, r.parent_id, r.latest_revision
            datum.setdefault(r.id, []).append(r)

        # create things
        ts = []
        for id in things.keys():
            name, parent_id, latest_revision = things[id]
            d, type = self._parse_data(datum[id])
            v = versions[id]
            t = Thing(self, id, name, self.withID(parent_id, lazy=True), latest_revision, v, type, d)
            v.thing = t
            ts.append(t)
        return ts

class ThingCache(LRU):
    """LRU Cache for storing things. Key can be either id or (name, parent_id)."""
    def __init__(self, capacity):
        LRU.__init__(self, capacity)
        self.name2id = {}
        
    def __contains__(self, key):
        if isinstance(key, tuple):
            return key in self.name2id
        else:
            return LRU.__contains__(self, key)
        
    def __getitem__(self, key):
        if isinstance(key, tuple):
            key = self.name2id[key]
        value = LRU.__getitem__(self, key)
        return value.copy()
        
    def get(self, key, default=None):
        if key in self:
            return self[key]
        else:
            return None
            
    def __setitem__(self, key, value):
        key = value.id
        LRU.__setitem__(self, key, value)
        # name2id mapping must be updated whenever a thing is added to the cache
        self.name2id[value.name, value.parent.id] = value.id
    
    def remove_node(self, node=None):
        node = LRU.remove_node(self.node)
        thing = node.value
        # when a node is removed, its corresponding entry 
        # from the name2id map must also be removed 
        del self.name2id[thing.name, thing.parent.id]
        return node
                    
class ProxyTDBImpl:
    """A TDB impl, which contains another TDB impl."""
    
    def __init__(self, impl):
        self.impl = impl
        self.set_parent(self)
        self.hints = web.storage()
        
    def set_parent(self, impl):
        self.parent = impl
        self.impl.set_parent(impl)

    def __getattr__(self, key):
        return getattr(self.impl, key)

class CachedTDBImpl(ProxyTDBImpl):
    """TDB with cache"""
    def __init__(self, impl):
        ProxyTDBImpl.__init__(self, impl)

        #@@ make this configurable
        self.cache = ThingCache(10000)
        self.querycache = LRU(1000)
                
    def withID(self, id, revision=None, lazy=False):
        if lazy:
            return LazyThing(lambda: self.withID(id, revision, lazy=False), id=id)
            
        if revision is not None:
            return self.impl.withID(id, revision)
            
        if id not in self.cache:
            self.cache[id] = self.impl.withID(id, revision)
            
        return self.cache[id]
    
    def withName(self, name, parent, revision=None, lazy=False):
        if lazy:
            return LazyThing(lambda: self.withName(name, parent, revision, lazy=False), name=name, parent=parent)
            
        if revision is not None:
            return self.impl.withName(name, parent, revision)
            
        if (name, parent.id) not in self.cache:
            self.cache[name, parent.id] = self.impl.withName(name, parent, revision)
        return self.cache[name, parent.id]
            
    def withIDs(self, ids, lazy=False):
        notincache = [id for id in ids if id not in self.cache]
        if notincache:
            for t in self.impl.withIDs(ids):
                self.cache[t.id] = t
        return [self.cache[id] for id in ids]
        
    def withNames(self, names, parent, lazy=False):
        notincache = [name for name in names if (name, parent.id) not in self.cache]
        if notincache:
            for t in self.impl.withNames(notincache, parent):
                self.cache[t.id] = t
        return [self.cache[name, parent.id] for name in names]

    def Things(self, limit=None, **query):
        q = dict(query, limit=limit)
        q = tuple(sorted(q.items()))
        
        try:
            if q not in self.querycache:
                self.querycache[q] = Things(self.parent, limit=limit, **query)
        except TypeError:
            # newly created Things will not have any id, so __hash__ will fail.
            # This is a work-around for that.
            return Things(self.parent, limit=limit, **query)
            
        return self.querycache[q]

    def Versions(self, limit=None, lazy=True, **query):
        if lazy:
            return LazyProxy(lambda: self.Versions(limit=limit, lazy=False, **query))
        else:    
            q = dict(query, limit=limit)
            q = tuple(sorted(q.items()))

            try:
                if q not in self.querycache:
                    self.querycache[q] = Versions(self.parent, limit=limit, **query)
            except TypeError:
                # newly created Things will not have any id, so __hash__ will fail.
                # This is a work-around for that.
                return Versions(self, limit=limit, **query)
            
            return self.querycache[q]

    def save(self, thing, author=None, comment='', ip=None):
        # previous version of thing
        #@@ what if previous version exists and not available in cache?
        self.impl.save(thing, author=author, comment=comment, ip=ip)
        
        old_thing = self.cache.get(thing.id)
        
        # expire queries effected by this save
        for k, q in self.querycache.items():
            if q.matches(thing) or (old_thing and q.matches(old_thing)):
                del self.querycache[k]
        
        # History query is made before the cache is cleared. That must be reassigned.
        thing.h = History(self, thing.id)
        self.cache[thing.id] = thing
                                
class RestrictedTDBImpl(ProxyTDBImpl):
    """TDB implementation to run in a restricted environment.
    As of now, it supports system and user modes of execution.
    In system mode all operations are permitted. 
    In user mode all but save operation is permitted.
    """
    def __init__(self, impl):
        ProxyTDBImpl.__init__(self, impl)
            
    def save(self, *a, **kw):
        mode = getattr(self.hints, 'mode', 'system')
        if mode == 'user':
            raise SecurityError, 'Permission Denied.'
        else:
            self.impl.save(*a, **kw)    
        
# hooks can be registered by extending the hook class
hooks = []
class metahook(type):
    def __init__(self, name, bases, attrs):
        hooks.append(self())
        type.__init__(self, name, bases, attrs)
        
class hook:
    __metaclass__ = metahook

#remove hook from hooks    
hooks.pop()

def _run_hooks(name, thing):
    for h in hooks:
        m = getattr(h, name, None)
        if m:
            m(thing)
