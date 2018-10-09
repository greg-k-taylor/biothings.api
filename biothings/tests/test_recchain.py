import unittest
from biothings.hub.datatransform.record_chain import RecChain


class TestRecChain(unittest.TestCase):

    def test_construction(self):
        doc_lst = [
            {
                '_id': 1
                },
            {
                '_id': 2
                },
            {
                '_id': 3
                }
            ]
        
        rc = RecChain('_id', doc_lst)
        rc.add_obj(1, 2)
        rc.add_obj(2, 3)
        rc.add_obj(3, 4)
        rc.add_obj(3, 5)

        # Perform checks on the results
        rci = rc.__iter__()
        n = next(rci)
        self.assertEquals(n, (1, 4))
        n = next(rci)
        self.assertEquals(n, (1, 5))
