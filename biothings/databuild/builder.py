import sys
import os.path
import time
import copy
import importlib
from datetime import datetime
from pprint import pformat
import logging

from biothings.utils.common import (timesofar, ask, safewfile,
                                    dump2gridfs, get_timestamp, get_random_string,
                                    setup_logfile, loadobj, get_class_from_classpath)
from biothings.utils.mongo import doc_feeder
from utils.es import ESIndexer
import biothings.databuild.backend as btbackend
from biothings.databuild.mapper import TransparentMapper


class BuilderException(Exception):
    pass
class ResumeException(Exception):
    pass


class DataBuilder(object):

    def __init__(self, build_name, source_backend, target_backend, log_folder,
                 doc_root_key=None, parallel_engine=None, max_build_status=10,
                 id_mappers=[], default_mapper_class=TransparentMapper,
                 sources=None, target_name=None,**kwargs):
        self.build_name = build_name
        self.source_backend = source_backend
        self.target_backend = target_backend
        self.doc_root_key = doc_root_key
        self.t0 = time.time()
        self.logfile = None
        self.log_folder = log_folder
        self.id_mappers = {}
        self.timestamp = datetime.now()

        for mapper in id_mappers + [default_mapper_class()]:
            self.id_mappers[mapper.name] = mapper

        self.step = kwargs.get("step",10000)
        self.parallel_engine = parallel_engine
        # max no. of records kept in "build" field of src_build collection.
        self.max_build_status = max_build_status
        self._build_config = self.source_backend.get_build_configuration(build_name)
        self.prepare(sources,target_name)
        self.setup_log()

    def setup_log(self):
        import logging as logging_mod
        if not os.path.exists(self.log_folder):
            os.makedirs(self.log_folder)
        self.logfile = os.path.join(self.log_folder, '%s_%s_build.log' % (self.build_name,time.strftime("%Y%m%d",self.timestamp.timetuple())))
        fh = logging_mod.FileHandler(self.logfile)
        fh.setFormatter(logging_mod.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        fh.name = "logfile"
        sh = logging_mod.StreamHandler()
        sh.name = "logstream"
        self.logger = logging_mod.getLogger("%s_build" % self.build_name)
        self.logger.setLevel(logging_mod.DEBUG)
        if not fh.name in [h.name for h in self.logger.handlers]:
            self.logger.addHandler(fh)
        if not sh.name in [h.name for h in self.logger.handlers]:
            self.logger.addHandler(sh)

    def register_status(self,status,transient=False,init=False,**extra):
        assert self._build_config, "build_config needs to be specified first"
        # get it from source_backend, kind of weird...
        src_build = self.source_backend.build
        build_info = {
             'status': status,
             'started_at': datetime.now(),
             'logfile': self.logfile,
             'target_backend': self.target_backend.name,
             'target_name': self.target_backend.target_name}
        if transient:
            # record some "in-progress" information
            build_info['pid'] = os.getpid()
        else:
            # only register time when it's a final state
            t1 = round(time.time() - self.t0, 0)
            build_info["time"] = timesofar(self.t0)
            build_info["time_in_s"] = t1
        # merge extra at root or "build" level
        # (to keep building data...)
        # it also means we want to merge the last one in "build" list
        self.logger.info("Registered status:\n%s" % pformat(build_info))
        _cfg = src_build.find_one({'_id': self._build_config['_id']})
        if "build" in extra:
            build_info.update(extra["build"])
            _cfg["build"][-1].update(build_info)
            src_build.replace_one({'_id': self._build_config['_id']},_cfg)
        # create a new build entre at the end and clean extra one (not needed/wanted)
        if init:
            src_build.update({'_id': self._build_config['_id']}, {"$push": {'build': build_info}})
            if len(_cfg['build']) > self.max_build_status:
                howmany = len(_cfg['build']) - self.max_build_status
                #remove any status not needed anymore
                for _ in range(howmany):
                    src_build.update({'_id': self._build_config['_id']}, {"$pop": {'build': -1}})

    def init_mapper(self,id_type):
        if self.id_mappers[id_type].need_load():
            self.logger.info("Initializing mapper '%s'" % id_type)
            self.id_mappers[id_type].load()

    def generate_document_query(self, src_name):
        return None

    def get_root_document_sources(self):
        return self._build_config.get(self.doc_root_key,[])

    def prepare(self,sources=None, target_name=None):
        self.source_backend.validate_sources(sources)
        self.target_backend.set_target_name(target_name, self.build_name)
        # root key is optional but if set, it must exist in build config
        if self.doc_root_key and not self.doc_root_key in self._build_config:
            raise BuilderException("Root document key '%s' can't be found in build configuration" % self.doc_root_key)

    def merge(self, sources=None, target_name=None, batch_size=100000,):

        self.t0 = time.time()
        # normalize
        if sources is None:
            self.target_backend.drop()
            self.target_backend.prepare()
            sources = self._build_config['sources']
        elif isinstance(sources,str):
            sources = [sources]

        if target_name:
            self.target_backend.set_target_name(target_name)

        try:
            self.register_status("building",transient=True,init=True,
                                 build={"step":"init","sources":sources})
            if self.parallel_engine:
                if sources:
                    raise NotImplemented("merge speficic sources not supported when using parallel")
                raise NotImplementedError("not yet")
            else:
                _stats = self.merge_sources(source_names=sources, batch_size=batch_size)
            self.target_backend.post_merge()

            _src_versions = self.source_backend.get_src_versions()
            self.register_status('success',build={"stats" : _stats, "src_versions" : _src_versions})

        except (KeyboardInterrupt,Exception) as e:
            import traceback
            self.logger.error(traceback.format_exc())
            self.register_status("failed",build={"err": repr(e)})
            raise

        finally:
            #do a simple validation here
            if getattr(self, '_stats', None):
                self.logger.info("Validating...")
                target_cnt = self.target_backend.count()
                if target_cnt == self._stats['total_documents']:
                    self.logger.info("OK [total count={}]".format(target_cnt))
                else:
                    self.logger.info("Warning: total count of gene documents does not match [{}, should be {}]".format(target_cnt, self._stats['total_genes']))

    def merge_resume(self):
        src_build = self.source_backend.build
        cfg = src_build.find_one({'_id': self._build_config['_id']})
        if len(cfg.get("build",[])) == 0:
            raise ResumeException("No build bound for configuration '%s', can't resume" % cfg["name"])
        # resume on latest build
        build = cfg["build"][-1]
        # first make sure we can actually resume
        sources = build.get("sources")
        if not sources:
            raise ResumeException("No 'sources' found")
        step = build.get("step")
        if not step:
            raise ResumeException("No 'step' found")
        if not step in sources:
            raise ResumeException("Step '%s' isn't part of sources used for the merge ('%s')" % (step,sources))
        target_name = build.get("target_name")
        if not target_name:
            raise ResumeException("No target_name found")
        status = build.get("status","success") # default: we don't care, we resume
        if not status == "failed":
            raise ResumeException("Nothing to resume")
        # compute new sources remaining for processing
        # note: current step has failed so it's part of the resume
        sources = sources[sources.index(step):]
        # re-validate source just in case
        self.source_backend.validate_sources(sources)
        self.logger.info("Resuming build: %s" % build)
        self.merge(sources,target_name)


    def validate(self, build_config='mygene_allspecies', n=10):
        '''Validate merged genedoc, currently for ES backend only.'''
        import random
        import itertools
        import pyes

        self.load_build_config(build_config)
        last_build = self._build_config['build'][-1]
        self.logger.info("Last build record:")
        self.logger.info(pformat(last_build))
        #assert last_build['target_backend'] == 'es', '"validate" currently works for "es" backend only'

        target_name = last_build['target']
        self.source.backend.validate_sources()
        self.prepare_target(target_name=target_name)
        self.logger.info("Validating...")
        target_cnt = self.target.count()
        stats_cnt = last_build['stats']['total_genes']
        if target_cnt == stats_cnt:
            self.logger.info("OK [total count={}]".format(target_cnt))
        else:
            self.logger.info("Warning: total count of gene documents does not match [{}, should be {}]".format(target_cnt, stats_cnt))

        if n > 0:
            for src in self._build_config['sources']:
                self.logger.info("\nSrc: %s" % src)
                # if 'id_type' in self.src_master[src] and self.src_master[src]['id_type'] != 'entrez_gene':
                #     print "skipped."
                #     continue
                cnt = self.src_db[src].count()
                fdr1 = doc_feeder(self.src_db[src], step=10000, s=cnt - n, logger=self.logger)
                rand_s = random.randint(0, cnt - n)
                fdr2 = doc_feeder(self.src_db[src], step=n, s=rand_s, e=rand_s + n, logger=self.logger)
                _first_exception = True
                for doc in itertools.chain(fdr1, fdr2):
                    _id = doc['_id']
                    try:
                        es_doc = self.target.get_from_id(_id)
                    except pyes.exceptions.NotFoundException:
                        if _first_exception:
                            self.logger.info("")
                            _first_exception = False
                        self.logger.info("%s not found." % _id)
                        continue
                    for k in doc:
                        if src == 'entrez_homologene' and k == 'taxid':
                            # there is occasionally known error for taxid in homologene data.
                            continue
                        assert es_doc.get(k, None) == doc[k], (_id, k, es_doc.get(k, None), doc[k])


    def get_mapper_for_source(self,src_name):
        id_type = self.source_backend.get_src_master_docs()[src_name].get('id_type')
        try:
            self.init_mapper(id_type)
            mapper = self.id_mappers[id_type]
            self.logger.info("Found mapper '%s' for source '%s'" % (mapper,src_name))
            return mapper
        except KeyError:
            raise BuilderException("Found id_type '%s' but no mapper associated" % id_type)

    def merge_sources(self, source_names, batch_size=100000):
        """
        Merge resources from given source_names or from build config.
        Identify root document sources from the list to first process them.
        """
        total_docs = 0
        _stats = {}
        # try to identify root document sources amongst the list to first
        # process them (if any)
        root_sources = list(set(source_names).intersection(set(self.get_root_document_sources())))
        other_sources = list(set(source_names).difference(set(root_sources)))
        # now re-order
        source_names = root_sources + other_sources

        self.logger.info("Merging following sources: %s" % repr(source_names))

        for i,src_name in enumerate(source_names):
            #if src_name in self._build_config.get(self.doc_root_key,[]):
            #    continue
            progress = "%s/%s" % (i+1,len(source_names))
            self.register_status("success",transient=True,
                                 build={"step":src_name,"progress":progress})
            src_stats =self.merge_source(src_name, batch_size=batch_size)
            _stats.update(src_stats)

        self.target_backend.finalize()

        return _stats

    def clean_document_to_merge(self,doc):
        return doc

    def merge_source(self, src_name, batch_size=100000):

        mapper = self.get_mapper_for_source(src_name)
        cnt = 0
        _query = self.generate_document_query(src_name)
        # Note: no need to check if there's an existing document with _id (we want to merge only with an existing document)
        # if the document doesn't exist then the update() call will silently fail.
        # That being said... if no root documents, then there won't be any previously inserted
        # documents, and this update() would just do nothing. So if no root docs, then upsert
        # (update or insert, but do something)
        upsert = (self.doc_root_key is None) or src_name in self.get_root_document_sources()
        if not upsert:
            self.logger.debug("Documents from source '%s' will be stored only if a previous document exist with same _id" % src_name)
        for docs in doc_feeder(self.source_backend[src_name], inbatch=True,
                               step=batch_size, logger=self.logger, query=_query):
            # prepare batch
            docs = mapper.process(docs)
            newdocs = map(self.clean_document_to_merge,docs)
            cnt += self.target_backend.update(newdocs, upsert=upsert)

        return {"total_%s" % src_name : cnt}


    #def _merge_ipython_cluster(self, step=100000):
    #    '''Do the merging on ipython cluster.'''
    #    from ipyparallel import Client, require
    #    from config import CLUSTER_CLIENT_JSON

    #    t0 = time.time()
    #    source_names = [collection for collection in self._build_config['sources']
    #                           if collection not in ['entrez_gene', 'ensembl_gene']]

    #    self.target.drop()
    #    self.target.prepare()
    #    geneid_set = self.make_doc_root()

    #    idmapping_gridfs_d = self._save_idmapping_gridfs()

    #    self.logger.info(timesofar(t0))

    #    rc = Client(CLUSTER_CLIENT_JSON)
    #    lview = rc.load_balanced_view()
    #    self.logger.info("\t# nodes in use: {}".format(len(lview.targets or rc.ids)))
    #    lview.block = False
    #    kwargs = {}
    #    target_collection = self.target.target_collection
    #    kwargs['server'], kwargs['port'] = target_collection.database.client.address
    #    kwargs['src_db'] = self.src_db.name
    #    kwargs['target_db'] = target_collection.database.name
    #    kwargs['target_collection_name'] = target_collection.name
    #    kwargs['limit'] = step

    #    @require('pymongo', 'time', 'types')
    #    def worker(kwargs):
    #        server = kwargs['server']
    #        port = kwargs['port']
    #        src_db = kwargs['src_db']
    #        target_db = kwargs['target_db']
    #        target_collection_name = kwargs['target_collection_name']

    #        src_collection = kwargs['src_collection']
    #        skip = kwargs['skip']
    #        limit = kwargs['limit']

    #        def load_from_gridfs(filename, db):
    #            import gzip
    #            import pickle
    #            import gridfs
    #            fs = gridfs.GridFS(db)
    #            fobj = fs.get(filename)
    #            gzfobj = gzip.GzipFile(fileobj=fobj)
    #            try:
    #                object = pickle.load(gzfobj)
    #            finally:
    #                gzfobj.close()
    #                fobj.close()
    #            return object

    #        def alwayslist(value):
    #            if value is None:
    #                return []
    #            if isinstance(value, (list, tuple)):
    #                return value
    #            else:
    #                return [value]

    #        conn = pymongo.MongoClient(server, port)
    #        src = conn[src_db]
    #        target_collection = conn[target_db][target_collection_name]

    #        idmapping_gridfs_name = kwargs.get('idmapping_gridfs_name', None)
    #        if idmapping_gridfs_name:
    #            idmapping_d = load_from_gridfs(idmapping_gridfs_name, src)
    #        else:
    #            idmapping_d = None

    #        cur = src[src_collection].find(skip=skip, limit=limit, timeout=False)
    #        cur.batch_size(1000)
    #        try:
    #            for doc in cur:
    #                _id = doc['_id']
    #                if idmapping_d:
    #                    _id = idmapping_d.get(_id, None) or _id
    #                # there could be cases that idmapping returns multiple entrez_gene id.
    #                for __id in alwayslist(_id): 
    #                    __id = str(__id)
    #                    doc.pop('_id', None)
    #                    doc.pop('taxid', None)
    #                    target_collection.update({'_id': __id}, doc, manipulate=False, upsert=False)
    #                    #target_collection.update({'_id': __id}, {'$set': doc},
    #        finally:
    #            cur.close()

    #    t0 = time.time()
    #    task_list = []
    #    for src_collection in source_names:
    #        _kwargs = copy.copy(kwargs)
    #        _kwargs['src_collection'] = src_collection
    #        id_type = self.src_master[src_collection].get('id_type', None)
    #        if id_type:
    #            idmapping_gridfs_name = idmapping_gridfs_d[id_type]
    #            _kwargs['idmapping_gridfs_name'] = idmapping_gridfs_name
    #        cnt = self.src_db[src_collection].count()
    #        for s in range(0, cnt, step):
    #            __kwargs = copy.copy(_kwargs)
    #            __kwargs['skip'] = s
    #            task_list.append(__kwargs)

    #    self.logger.info("\t# of tasks: {}".format(len(task_list)))
    #    self.logger.info("\tsubmitting...")
    #    job = lview.map_async(worker, task_list)
    #    self.logger.info("done.")
    #    job.wait_interactive()
    #    self.logger.info("\t# of results returned: {}".format(len(job.result())))
    #    self.logger.info("\ttotal time: {}".format(timesofar(t0)))

    #    if self.shutdown_ipengines_after_done:
    #        self.logger.info("\tshuting down all ipengine nodes...")
    #        lview.shutdown()
    #        self.logger.info('Done.')


    #def _merge_parallel(self, collection, geneid_set, step=100000, idmapping_d=None):
    #    from multiprocessing import Process, Queue
    #    NUMBER_OF_PROCESSES = 8

    #    input_queue = Queue()
    #    input_queue.conn_pool = []

    #    def worker(q, target):
    #        while True:
    #            doc = q.get()
    #            if doc == 'STOP':
    #                break
    #            __id = doc.pop('_id')
    #            doc.pop('taxid', None)
    #            target.update(__id, doc)
    #            # target_collection.update({'_id': __id}, {'$set': doc},
    #            #                           manipulate=False,
    #            #                           upsert=False) #,safe=True)

    #    # Start worker processes
    #    for i in range(NUMBER_OF_PROCESSES):
    #        Process(target=worker, args=(input_queue, self.target)).start()

    #    for doc in doc_feeder(self.src_db[collection], step=step):
    #        _id = doc['_id']
    #        if idmapping_d:
    #            _id = idmapping_d.get(_id, None) or _id
    #        for __id in alwayslist(_id):    # there could be cases that idmapping returns multiple entrez_gene ids.
    #            __id = str(__id)
    #            if __id in geneid_set:
    #                doc['_id'] = __id
    #                input_queue.put(doc)

    #    # Tell child processes to stop
    #    for i in range(NUMBER_OF_PROCESSES):
    #        input_queue.put('STOP')

    #def _merge_parallel_ipython(self, collection, geneid_set, step=100000, idmapping_d=None):
    #    from IPython.parallel import Client, require

    #    rc = Client()
    #    dview = rc[:]
    #    #dview = rc.load_balanced_view()
    #    dview.block = False
    #    target_collection = self.target.target_collection
    #    dview['server'], dview['port'] = target_collection.database.client.address
    #    dview['database'] = target_collection.database.name
    #    dview['collection_name'] = target_collection.name

    #    def partition(lst, n):
    #        q, r = divmod(len(lst), n)
    #        indices = [q * i + min(i, r) for i in range(n + 1)]
    #        return [lst[indices[i]:indices[i + 1]] for i in range(n)]

    #    @require('pymongo', 'time')
    #    def worker(doc_li):
    #        conn = pymongo.MongoClient(server, port)
    #        target_collection = conn[database][collection_name]
    #        t0 = time.time()
    #        for doc in doc_li:
    #            __id = doc.pop('_id')
    #            doc.pop('taxid', None)
    #            target_collection.update({'_id': __id}, {'$set': doc},
    #                                     manipulate=False,
    #                                     upsert=False)  # ,safe=True)
    #        self.logger.info('Done. [%.1fs]' % (time.time() - t0))

    #    for doc in doc_feeder(self.src_db[collection], step=step):
    #        _id = doc['_id']
    #        if idmapping_d:
    #            _id = idmapping_d.get(_id, None) or _id
    #        for __id in alwayslist(_id):    # there could be cases that idmapping returns multiple entrez_gene ids.
    #            __id = str(__id)
    #            if __id in geneid_set:
    #                doc['_id'] = __id
    #                self.doc_queue.append(doc)

    #                if len(self.doc_queue) >= step:
    #                    #dview.scatter('doc_li', self.doc_queue)
    #                    #dview.apply_async(worker)
    #                    dview.map_async(worker, partition(self.doc_queue, len(rc.ids)))
    #                    self.doc_queue = []
    #                    self.logger.info("!")

    def get_last_src_build_stats(self):
        src_build = getattr(self, 'src_build', None)
        if src_build:
            _cfg = src_build.find_one({'_id': self._build_config['_id']})
            if _cfg['build'][-1].get('status', None) == 'success' and \
               _cfg['build'][-1].get('stats', None):
                stats = _cfg['build'][-1]['stats']
                return stats

    def get_target_collection(self):
        '''get the lastest target_collection from src_build record.'''
        src_build = getattr(self, 'src_build', None)
        if src_build:
            _cfg = src_build.find_one({'_id': self._build_config['_id']})
            if _cfg['build'][-1].get('status', None) == 'success' and \
               _cfg['build'][-1].get('target', None):
                target_collection = _cfg['build'][-1]['target']
                _db = get_target_db()
                target_collection = _db[target_collection]
                return target_collection

    def pick_target_collection(self, autoselect=True):
        '''print out a list of available target_collection, let user to pick one.'''
        target_db = get_target_db()
        target_collection_prefix = 'genedoc_' + self._build_config['name']
        target_collection_list = [target_db[name] for name in sorted(target_db.collection_names()) if name.startswith(target_collection_prefix)]
        if target_collection_list:
            self.logger.info("Found {} target collections:".format(len(target_collection_list)))
            self.logger.info('\n'.join(['\t{0:<5}{1.name:<45}\t{2}'.format(
                str(i + 1) + ':', target, target.count()) for (i, target) in enumerate(target_collection_list)]))
            self.logger.info("")
            while 1:
                if autoselect:
                    selected_idx = input("Pick one above [{}]:".format(len(target_collection_list)))
                else:
                    selected_idx = input("Pick one above:")
                if autoselect:
                    selected_idx = selected_idx or len(target_collection_list)
                try:
                    selected_idx = int(selected_idx)
                    break
                except ValueError:
                    continue
            return target_collection_list[selected_idx - 1]
        else:
            self.logger.info("Found no target collections.")

    def get_mapping(self, enable_timestamp=True):
        '''collect mapping data from data sources.
           This is for DocESBackend only.
        '''
        mapping = {}
        src_master = get_src_master(self.src_db.client)
        for collection in self._build_config['sources']:
            meta = src_master.find_one({"_id" : collection})
            if 'mapping' in meta:
                mapping.update(meta['mapping'])
            else:
                self.logger.info('Warning: "%s" collection has no mapping data.' % collection)
        mapping = {"properties": mapping,
                   "dynamic": False}
        if enable_timestamp:
            mapping['_timestamp'] = {
                "enabled": True,
            }
        #allow source Compression
        #Note: no need of source compression due to "Store Level Compression"
        #mapping['_source'] = {'compress': True,}
        #                      'compress_threshold': '1kb'}
        return mapping

    def build_index(self, use_parallel=True):
        target_collection = self.get_target_collection()
        if target_collection:
            es_idxer = ESIndexer(mapping=self.get_mapping())
            es_idxer.ES_INDEX_NAME = 'genedoc_' + self._build_config['name']
            es_idxer.step = 10000
            es_idxer.use_parallel = use_parallel
            #es_idxer.s = 609000
            #es_idxer.conn.indices.delete_index(es_idxer.ES_INDEX_NAME)
            es_idxer.create_index()
            es_idxer.delete_index_type(es_idxer.ES_INDEX_TYPE, noconfirm=True)
            es_idxer.build_index(target_collection, verbose=False)
            es_idxer.optimize()
        else:
            self.logger.info("Error: target collection is not ready yet or failed to build.")

    def build_index2(self, build_config='mygene_allspecies', last_build_idx=-1, use_parallel=False, es_host=None, es_index_name=None, noconfirm=False):
        """Build ES index from last successfully-merged mongodb collection.
            optional "es_host" argument can be used to specified another ES host, otherwise default ES_HOST.
            optional "es_index_name" argument can be used to pass an alternative index name, otherwise same as mongodb collection name
        """
        self.load_build_config(build_config)
        assert "build" in self._build_config, "Abort. No such build records for config %s" % build_config
        last_build = self._build_config['build'][last_build_idx]
        self.logger.info("Last build record:")
        self.logger.info(pformat(last_build))
        assert last_build['status'] == 'success', \
            "Abort. Last build did not success."
        assert last_build['target_backend'] == "mongo", \
            'Abort. Last build need to be built using "mongo" backend.'
        assert last_build.get('stats', None), \
            'Abort. Last build stats are not available.'
        self._stats = last_build['stats']
        assert last_build.get('target', None), \
            'Abort. Last build target_collection is not available.'

        # Get the source collection to build the ES index
        # IMPORTANT: the collection in last_build['target'] does not contain _timestamp field,
        #            only the "genedoc_*_current" collection does. When "timestamp" is enabled
        #            in mappings, last_build['target'] collection won't be indexed by ES correctly,
        #            therefore, we use "genedoc_*_current" collection as the source here:
        #target_collection = last_build['target']
        target_collection = "genedoc_{}_current".format(build_config)
        _db = get_target_db()
        target_collection = _db[target_collection]
        self.logger.info("")
        self.logger.info('Source: %s' % target_collection.name)
        _mapping = self.get_mapping()
        _meta = {}
        src_version = self.source_backend.get_src_versions()
        if src_version:
            _meta['src_version'] = src_version
        if getattr(self, '_stats', None):
            _meta['stats'] = self._stats
        if 'timestamp' in last_build:
            _meta['timestamp'] = last_build['timestamp']
        if _meta:
            _mapping['_meta'] = _meta
        es_index_name = es_index_name or target_collection.name
        es_idxer = ESIndexer(mapping=_mapping,
                             es_index_name=es_index_name,
                             es_host=es_host,
                             step=5000)
        if build_config == 'mygene_allspecies':
            es_idxer.number_of_shards = 10   # default 5
        es_idxer.check()
        if noconfirm or ask("Continue to build ES index?") == 'Y':
            es_idxer.use_parallel = use_parallel
            #es_idxer.s = 609000
            if es_idxer.exists_index(es_idxer.ES_INDEX_NAME):
                if noconfirm or ask('Index "{}" exists. Delete?'.format(es_idxer.ES_INDEX_NAME)) == 'Y':
                    es_idxer.conn.indices.delete(es_idxer.ES_INDEX_NAME)
                else:
                    self.logger.info("Abort.")
                    return
            es_idxer.create_index()
            #es_idxer.delete_index_type(es_idxer.ES_INDEX_TYPE, noconfirm=True)
            es_idxer.build_index(target_collection, verbose=False)
            # time.sleep(10)    # pausing 10 second here
            # if es_idxer.wait_till_all_shards_ready():
            #     print "Optimizing...", es_idxer.optimize()

    #def sync_index(self, use_parallel=True):
    #    from utils import diff

    #    sync_src = self.get_target_collection()

    #    es_idxer = ESIndexer(self.get_mapping())
    #    es_idxer.ES_INDEX_NAME = sync_src.target_collection.name
    #    es_idxer.step = 10000
    #    es_idxer.use_parallel = use_parallel
    #    sync_target = btbackend.DocESBackend(es_idxer)

    #    changes = diff.diff_collections(sync_src, sync_target)
    #    return changes

