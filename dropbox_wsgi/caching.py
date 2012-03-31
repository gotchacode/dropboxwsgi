import errno
import itertools
import operator
import os
import logging
import shutil
import tempfile

try:
    import json
except Exception:
    import simplejson as json

from wsgiref.util import FileWrapper

logger = logging.getLogger(__name__)

class FileSystemCache(object):
    TAG_NAME = 'tag.txt'
    DATA_NAME = 'data.bin'
    DIR_INTER = 'dir'

    def __init__(self, app_dir):
        self.tmp_dir = os.path.join(app_dir, 'tmp')
        self.cache_dir = os.path.join(app_dir, 'cache')

        # if these fail, let the exception raise
        # TODO: blow these away if they are files
        # TODO: check permissions
        for p in [self.cache_dir, self.tmp_dir]:
            self._makedirs(p)

    def _generate_cache_path(self, path):
        top = self.cache_dir
        pieces = path.split('/')

        def splice_after(pieces):
            for j in pieces:
                yield j
                yield self.DIR_INTER

        parent_dir = os.path.join(top, *splice_after(itertools.islice(pieces, 1, len(pieces) - 1)))

        return os.path.join(parent_dir, pieces[-1])

    @classmethod
    def _makedirs(cls, path):
        try:
            os.makedirs(path)
        except OSError, e:
            if e.errno != errno.EEXIST:
                raise
            elif not os.path.isdir(path):
                raise Exception("Not a directory: %r" % path)

    def read_cached_headers(self, path):
        cache_path = self._generate_cache_path(path)
        with open(os.path.join(cache_path, self.TAG_NAME), 'rb') as f:
            res = json.load(f)

        try:
            return [(k.encode('utf8'), v.encode('utf8')) for (k, v) in res]
        except Exception:
            logger.exception("Bad data in metadata file!")
            self.drop_cached_data(path)
            raise

    def drop_cached_data(self, path):
        try:
            shutil.rmtree(self._generate_cache_path(path))
        except EnvironmentError, e:
            if e.errno != errno.ENOENT:
                raise

    def read_cached_data(self, path):
        cache_path = self._generate_cache_path(path)
        return open(os.path.join(cache_path, self.DATA_NAME), 'rb')

    def write_cached_data(self, path, headers):
        s1 = self
        class NoOp(object):
            def __init__(self):
                fd, self.path = tempfile.mkstemp(dir=s1.tmp_dir)
                self.f = os.fdopen(fd, 'wb')

            def write(self, data):
                self.f.write(data)

            def done(self):
                unlink = True
                try:
                    self.f.close()
                    self.f = None

                    tmp_source_path = tempfile.mkdtemp(dir=s1.tmp_dir)

                    os.rename(self.path, os.path.join(tmp_source_path, s1.DATA_NAME))
                    unlink = False
                    with open(os.path.join(tmp_source_path, s1.TAG_NAME), 'wb') as f:
                        json.dump(headers, f)

                    cache_path = s1._generate_cache_path(path)
                    try:
                        shutil.rmtree(cache_path)
                    except EnvironmentError, e:
                        if e.errno != errno.ENOENT:
                            logger.exception("Couldn't remove %r before renaming over it", cache_path)

                    s1._makedirs(os.path.dirname(cache_path))
                    os.rename(tmp_source_path, cache_path)
                finally:
                    if unlink:
                        os.unlink(self.path)

            def close(self):
                if self.f is not None:
                    self.f.close()

            def __enter__(self):
                return self

            def __exit__(self, *n, **kw):
                self.close()

        return NoOp()

def py_methodcaller(method, *n, **kw):
    def mc(o):
        return getattr(o, method)(*n, **kw)
    return mc

try:
    methodcaller = operator.methodcaller
except AttributeError:
    methodcaller = py_methodcaller

def identity(a): return a

def get_from_alist(alist, k, key=identity):
    for (kc, vc) in alist:
        if key(kc) == k:
            return vc

def make_caching(impl):
    def wrapper(app):
        def new_app(environ, start_response):
            # if the client is already sending up
            # the caching headers then use that
            if ('HTTP_IF_MODIFIED_SINCE' in environ or
                'HTTP_IF_NONE_MATCH' in environ):
                return app(environ, start_response)

            path = environ['PATH_INFO']

            try:
                h = impl.read_cached_headers(path)
            except Exception, e:
                if not (isinstance(e, EnvironmentError) and e.errno == errno.ENOENT):
                    logger.exception("Couldn't read cached data")
            else:
                etag = get_from_alist(h, 'etag', key=methodcaller('lower'))
                if etag is not None:
                    environ['HTTP_IF_NONE_MATCH'] = etag

            writer = [None]
            def make_writer(headers):
                f = impl.write_cached_data(path, headers)
                try:
                    while True:
                        data = yield
                        if not data:
                            break
                        f.write(data)
                    f.done()
                finally:
                    f.close()

            top_res = []
            def my_start_response(code, headers):
                top_res[:] = [code]
                if code.startswith('304'):
                    def noop(_): pass
                    return noop
                else:
                    etag = None
                    if code.startswith('200'):
                        # save new data with etag if it exists
                        etag = get_from_alist(headers, 'etag', methodcaller('lower'))

                    if etag is not None:
                        # they are going to pass data into this thing,
                        # save it!!
                        top_writer = start_response(code, headers)
                        writer[0] = make_writer(headers)
                        writer[0].next()

                        def new_writer(data):
                            writer[0].send(data)
                            return top_writer(data)

                        return new_writer
                    else:
                        return start_response(code, headers)

            res = app(environ, my_start_response)
            if top_res[0].startswith('304'):
                logger.debug("Cache hit")
                # send out saved data
                start_response('200 OK', h)
                fwrapper = environ.get('wsgi.file_wrapper', FileWrapper)
                block_size = 16 * 1024
                toret = fwrapper(impl.read_cached_data(path), block_size)
            elif writer[0] is not None:
                logger.debug("Cache miss")
                # handle the rest of data for saving
                def better_res():
                    for d in res:
                        writer[0].send(d)
                        yield d
                    writer[0].send('')
                toret = better_res()
            else:
                toret = res

            return toret
        return new_app
    return wrapper
