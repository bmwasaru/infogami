from tdb import SimpleTDBImpl, CachedTDBImpl
from tdb import NotFound, hook, Thing

impl = SimpleTDBImpl()

root = impl.root
setup = impl.setup
withID = impl.withID
withName = impl.withName
withIDs = impl.withIDs
withNames = impl.withNames
new = impl.new
Things = impl.Things
Versions = impl.Versions
stats = impl.stats

