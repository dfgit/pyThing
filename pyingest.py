import pandas as pd
from neo4j import GraphDatabase
import yaml
import datetime
import sys
import gzip
from zipfile import ZipFile
from urllib.parse import urlparse
import boto3
from smart_open import open
import io
import pathlib
import ijson

config = dict()
supported_compression_formats = ['gzip', 'zip', 'none']

class LocalServer(object):

    def __init__(self):
        self._driver = GraphDatabase.driver(config['server_uri'],
                                            auth=(config['admin_user'],
                                                  config['admin_pass']))

    def close(self):
        self._driver.close()

    def load_file(self, file):

        # Set up parameters/defaults
        # Check skip_file first so we can exit early
        skip = file.get('skip_file') or False
        if skip:
            print("Skipping this file: {}".format(file['url']))
            return
        print_progress('Reading file:', "'" + file['url'] + "'")
        # If file type is specified, use that.  Else check the extension.  Else, treat as csv
        type = file.get('type') or 'NA'
        if type != 'NA':
            if type == 'csv':
                self.load_csv(file)
            elif type == 'json':
                self.load_json(file)
            else:
                print("Error! Can't process file because unknown type", type, "was specified")
        else:
            file_suffixes = pathlib.Path(file['url']).suffixes
            if '.csv' in file_suffixes:
                self.load_csv(file)
            elif '.json' in file_suffixes:
                self.load_json(file)
            else:
                self.load_csv(file)

        print_progress('Completed file:', "'" + file['url'] + "'")

    # Tells ijson to return decimal number as float.  Otherwise, it wraps them in a Decimal object,
    # which angers the Neo4j driver
    @staticmethod
    def ijson_decimal_as_float(events):
        for prefix, event, value in events:
            if event == 'number':
                value = str(value)
            yield prefix, event, value

    def load_json(self, file):
        with self._driver.session() as session:
            params = self.get_params(file)
            openfile = file_handle(params['url'], params['compression'])
            # 'item' is a magic word in ijson.  It just means the next-level element of an array
            items = ijson.common.items(self.ijson_decimal_as_float(ijson.parse(openfile)), 'item')
            # Next, pool these into array of 'chunksize'
            halt = False
            rec_num = 0
            chunk_num = 0
            rows = []
            while not halt:
                row = next(items, None)
                if row is None:
                    halt = True
                else:
                    rec_num = rec_num+1
                    if rec_num > params['skip_records']:
                        rows.append(row)
                        if len(rows) == params['chunk_size']:
                            print(file['url'], chunk_num, datetime.datetime.utcnow(), flush=True)
                            chunk_num = chunk_num + 1
                            rows_dict = {'rows': rows}
                            session.run(params['cql'], dict=rows_dict).consume()
                            rows = []

            if len(rows) > 0:
                print(file['url'], chunk_num, datetime.datetime.utcnow(), flush=True)
                rows_dict = {'rows': rows}
                session.run(params['cql'], dict=rows_dict).consume()

        # print("{} : Completed file", datetime.datetime.utcnow())

    def get_params(self, file):
        params = dict()
        params['skip_records'] = file.get('skip_records') or 0
        params['compression'] = file.get('compression') or 'none'
        if params['compression'] not in supported_compression_formats:
            print("Unsupported compression format: {}", params['compression'])

        params['url'] = file['url']
        # print("File", params['url'])
        params['cql'] = file['cql']
        params['chunk_size'] = file.get('chunk_size') or 1000
        params['field_sep'] = file.get('field_separator') or ','
        return params

    def load_csv(self, file):
        with self._driver.session() as session:
            params = self.get_params(file)

            openfile = file_handle(params['url'], params['compression'])

            # - The file interfaces should be consistent in Python but they aren't
            if params['compression'] == 'zip':
                header = openfile.readline().decode('UTF-8')
            else:
                header = str(openfile.readline())

            # Grab the header from the file and pass that to pandas.  This allow the header
            # to be applied even if we are skipping lines of the file
            header = header.strip().split(params['field_sep'])

            # Pandas' read_csv method is highly optimized and fast :-)
            row_chunks = pd.read_csv(openfile, dtype=str, sep=params['field_sep'], error_bad_lines=False,
                                     index_col=False, skiprows=params['skip_records'], names=header,
                                     low_memory=False, engine='c', compression='infer', header=None,
                                     chunksize=params['chunk_size'])

            for i, rows in enumerate(row_chunks):
                # print(params['url'], i, datetime.datetime.utcnow(), flush=True)
                # Chunk up the rows to enable additional fastness :-)
                rows_dict = {'rows': rows.fillna(value="").to_dict('records')}
                session.run(params['cql'],
                            dict=rows_dict).consume()

    def pre_ingest(self):
        if 'pre_ingest' in config:
            statements = config['pre_ingest']
            print_progress('Starting pre-ingest', '')
            with self._driver.session() as session:
                for statement in statements:
                    print_progress('Running:', statement)
                    session.run(statement)
            print_progress('Finished pre-ingest', '')

    def post_ingest(self):
        if 'post_ingest' in config:
            statements = config['post_ingest']
            print_progress('Starting post-ingest', '')
            with self._driver.session() as session:
                for statement in statements:
                    print_progress('Running:', statement)
                    session.run(statement)
            print_progress('Finished post-ingest', '')

def file_handle(url, compression):
    parsed = urlparse(url)
    if parsed.scheme == 's3':
        path = get_s3_client().get_object(Bucket=parsed.netloc, Key=parsed.path[1:])['Body']
    elif parsed.scheme == 'file':
        path = parsed.path
    else:
        path = url
    if compression == 'gzip':
        return gzip.open(path, 'rt')
    elif compression == 'zip':
        # Only support single file in ZIP archive for now
        if isinstance(path, str):
            buffer = path
        else:
            buffer = io.BytesIO(path.read())
        zf = ZipFile(buffer)
        filename = zf.infolist()[0].filename
        return zf.open(filename)
    else:
        return open(path)

def print_progress(msg, value=''):
    print("{:%Y-%m-%d %H:%M:%S}\t{}\t{}".format(datetime.datetime.now(), msg, value))

def get_s3_client():
    return boto3.Session().client('s3')


def load_config(configuration):
    global config
    with open(configuration) as config_file:
        config = yaml.load(config_file, yaml.SafeLoader)

def main():
    configuration = sys.argv[1]
    load_config(configuration)
    server = LocalServer()
    server.pre_ingest()
    file_list = config['files']
    print_progress("Using Server: ", config['server_uri'])
    for file in file_list:
        if 'basepath' in config:
            file['url'] = config['basepath'] + file['url']
        server.load_file(file)

    server.post_ingest()
    server.close()

if __name__ == "__main__":
    main()
