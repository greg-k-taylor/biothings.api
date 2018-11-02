from networkx import nx
from networkx import all_simple_paths
from biothings.hub.datatransform.utils import nested_lookup


class RecChain(object):
    """
    This class represents a tree structure from the start object
    to the final object
    """

    def __init__(self, field=None, doc_lst=None):
        self.graph = nx.DiGraph()
        if field and doc_lst:
            self._init_strct(field, doc_lst)

    def _init_strct(self, field, doc_lst):
        """initialze the network from a document list"""
        for doc in doc_lst:
            value = nested_lookup(doc, field)
            if value:
                self.add_root(value)

    def add_root(self, obj1):
        """Add obj1 to the graph as a root node"""
        self.graph.add_node(obj1)

    def add(self, obj1, obj2):
        """Add obj2 to the graph and then add an edge from obj1 to obj2"""
        # note: nx.DiGraph does not allow duplicate nodes or edges
        # this is preferred behavior here
        print("RecordChain.add({}, {})".format(obj1, obj2))
        self.graph.add_node(obj2)
        self.graph.add_edge(obj1, obj2)

    def __iadd__(self, other):
        """object += additional, which combines chains"""
        if not isinstance(other, RecChain):
            raise TypeError("other is not of type RecChain")
        for (obj1, obj2) in other:
            self.add(obj1, obj2)
        return self

    def __len__(self):
        """Return the number of root nodes"""
        return len(self.root_nodes())

    def __str__(self):
        """convert to a string, useful for debugging"""
        lst = []
        for r in self.root_nodes():
            for l in self.find_leaf(r):
                lst.append((r, l))
        return str(lst)

    @property
    def id_lst(self):
        """Build up a list of current ids"""
        id_set = set()
        for r in self.root_nodes():
            for l in self.find_leaf(r):
                id_set.add(l)
        return list(id_set)

    def lookup(self, obj1, obj2):
        """Find if a (left, right) pair is already in the list"""
        for r in self.find_left(obj1):
            if obj2 == r:
                return True
        return False

    def left(self, obj):
        """Determine if the obj is registered"""
        return obj in self.root_nodes()

    def find(self, where, ids):
        if not ids:
            return
        if not type(ids) in (list,tuple):
            ids = [ids]
        for id in ids:
            if id in where.keys():
                for i in where[id]:
                    yield i

    def find_left(self, ids):
        for id in ids:
            for leaf_node in self.find_leaf(id):
                yield leaf_node

    def right(self, id):
        """Determine if the id (_, right) is registered"""
        return id in self.leaf_nodes()

    def find_right(self, ids):
        """Find the first id founding by searching the (_, right) identifiers"""
        for id in ids:
            for root_node in self.find_root(id):
                yield root_node

    def root_nodes(self):
        root_nodes = [x for x in self.graph.nodes() if self.graph.out_degree(x) == 1 and self.graph.in_degree(x) == 0]
        return root_nodes

    def leaf_nodes(self):
        leaf_nodes = [x for x in self.graph.nodes() if self.graph.out_degree(x) == 0 and self.graph.in_degree(x) == 1]
        return leaf_nodes

    def list_paths(self, obj):
        """List all paths that start with obj"""
        leaf_nodes = [x for x in self.graph.nodes() if self.graph.out_degree(x) == 0 and self.graph.in_degree(x) == 1]

        for lf in leaf_nodes:
            for path in all_simple_paths(self.graph, self.start, lf):
                yield(path)

    def find_root(self, n):
        if self.graph.in_degree(n) == 0:
            yield n
        else:
            for v1, v2 in self.graph.in_edges(n):
                yield from self.find_leaf(v1)

    def find_leaf(self, n):
        if self.graph.out_degree(n) == 0:
            yield n
        else:
            for v1, v2 in self.graph.out_edges(n):
                yield from self.find_leaf(v2)

    def __iter__(self):
        """iterator overload function"""
        for r in self.root_nodes():
            for l in self.find_leaf(r):
                yield r, l
