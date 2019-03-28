import json
import logging
import os
import sys

from enum import Enum

import anymarkup
import click
import jsonschema
import requests

import yaml
try:
    from yaml import CLoader as yamlLoader, CDumper as yamlDumper
except ImportError:
    from yaml import Loader as yamlLoader, Dumper as yamlDumper

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

try:
    basestring
except NameError:
    basestring = str


class IncorrectSchema(Exception):
    def __init__(self, got, expecting):
        message = "incorrect schema: got `{}`, expecting `{}`".format(
            got, expecting)
        super(Exception, self).__init__(message)


class MissingSchemaFile(Exception):
    def __init__(self, path):
        self.path = path
        message = "schema not found: `{}`".format(path)
        super(Exception, self).__init__(message)


class ValidatedFileKind(Enum):
    SCHEMA = "SCHEMA"
    DATA_FILE = "FILE"
    REF = "REF"
    RESOURCE_PATH = "RESOURCE_PATH"


class ValidationResult(object):
    def summary(self):
        status = 'OK' if self.status else 'ERROR'
        summary = "{}: {}".format(status, self.filename)

        if hasattr(self, 'ref'):
            summary += " ({})".format(getattr(self, 'ref'))

        if hasattr(self, 'schema_url'):
            summary += " ({})".format(getattr(self, 'schema_url'))

        return summary


class ValidationOK(ValidationResult):
    status = True

    def __init__(self, kind, filename, schema_url):
        self.kind = kind
        self.filename = filename
        self.schema_url = schema_url

    def dump(self):
        return {
            "filename": self.filename,
            "kind": self.kind.value,
            "result": {
                "summary": self.summary(),
                "status": "OK",
                "schema_url": self.schema_url,
            }
        }


class ValidationRefOK(ValidationResult):
    status = True

    def __init__(self, kind, filename, ref, schema_url):
        self.kind = kind
        self.filename = filename
        self.schema_url = schema_url
        self.ref = ref

    def dump(self):
        return {
            "filename": self.filename,
            "ref": self.ref,
            "kind": self.kind.value,
            "result": {
                "summary": self.summary(),
                "status": "OK",
                "schema_url": self.schema_url,
                "ref": self.ref,
            }
        }


class ValidationError(ValidationResult):
    status = False

    def __init__(self, kind, filename, reason, error, **kwargs):
        self.kind = kind
        self.filename = filename
        self.reason = reason
        self.error = error
        self.kwargs = kwargs

    def dump(self):
        result = {
            "summary": self.summary(),
            "status": "ERROR",
            "reason": self.reason,
            "error": self.error.__str__()
        }

        result.update(self.kwargs)

        return {
            "filename": self.filename,
            "kind": self.kind.value,
            "result": result
        }

    def error_info(self):
        if self.error.message:
            msg = "{}\n{}".format(self.reason, self.error.message)
        else:
            msg = self.reason

        return msg


def get_handlers(schemas_bundle):
    """
    Generates a dictionary which will be used as an the `handlers` argument for
    jsonschema.RefResolver.

    `handlers` is a mapping from URI schemes to functions that should be used
    to retrieve them.

    In this case we are overloading the empty string scheme, which will be the
    scheme detected for absolute or relative file paths.
    """
    return {
        '': lambda x: schemas_bundle[x]
    }


def validate_schema(schemas_bundle, filename, schema_data):
    kind = ValidatedFileKind.SCHEMA

    logging.info('validating schema: {}'.format(filename))

    try:
        meta_schema_url = schema_data[u'$schema']
    except KeyError as e:
        return ValidationError(kind, filename, "MISSING_SCHEMA_URL", e)

    if meta_schema_url in schemas_bundle:
        meta_schema = schemas_bundle[meta_schema_url]
    else:
        meta_schema = fetch_schema(meta_schema_url)
        schemas_bundle[meta_schema_url] = meta_schema

    resolver = jsonschema.RefResolver(
        filename,
        schema_data,
        handlers=get_handlers(schemas_bundle)
    )

    try:
        jsonschema.Draft4Validator.check_schema(schema_data)
        validator = jsonschema.Draft4Validator(meta_schema, resolver=resolver)
        validator.validate(schema_data)
    except jsonschema.ValidationError as e:
        return ValidationError(kind, filename, "VALIDATION_ERROR", e,
                               meta_schema_url=meta_schema_url)
    except (jsonschema.SchemaError,
            jsonschema.exceptions.RefResolutionError) as e:
        return ValidationError(kind, filename, "SCHEMA_ERROR", e,
                               meta_schema_url=meta_schema_url)

    return ValidationOK(kind, filename, meta_schema_url)


def validate_file(schemas_bundle, filename, data):
    kind = ValidatedFileKind.DATA_FILE

    logging.info('validating file: {}'.format(filename))

    try:
        schema_url = data[u'$schema']
    except KeyError as e:
        return ValidationError(kind, filename, "MISSING_SCHEMA_URL", e)

    if not schema_url.startswith('http') and not schema_url.startswith('/'):
        schema_url = '/' + schema_url

    try:
        schema = schemas_bundle[schema_url]
    except KeyError as e:
        return ValidationError(kind, filename, "SCHEMA_NOT_FOUND", e,
                               schema_url=schema_url)

    try:
        resolver = jsonschema.RefResolver(
            schema_url,
            schema,
            handlers=get_handlers(schemas_bundle)
        )
        validator = jsonschema.Draft4Validator(schema, resolver=resolver)
        validator.validate(data)
    except jsonschema.ValidationError as e:
        return ValidationError(kind, filename, "VALIDATION_ERROR", e,
                               schema_url=schema_url)
    except jsonschema.SchemaError as e:
        return ValidationError(kind, filename, "SCHEMA_ERROR", e,
                               schema_url=schema_url)
    except TypeError as e:
        return ValidationError(kind, filename, "SCHEMA_TYPE_ERROR", e,
                               schema_url=schema_url)

    return ValidationOK(kind, filename, schema_url)


def validate_ref(schemas_bundle, bundle, filename, data, ptr, ref):
    kind = ValidatedFileKind.REF

    try:
        ref_data = bundle[ref["$ref"]]
    except KeyError as e:
        return ValidationError(
            kind,
            filename,
            "FILE_NOT_FOUND",
            e,
            ref=ref['$ref']
        )

    try:
        schema = schemas_bundle[data['$schema']]
    except KeyError as e:
        return ValidationError(
            kind,
            filename,
            "SCHEMA_NOT_FOUND",
            e,
            ref=ref['$ref']
        )

    try:
        schema_info = get_schema_info_from_pointer(schema, ptr)
    except KeyError as e:
        return ValidationError(
            kind,
            filename,
            "SCHEMA_DEFINITION_NOT_FOUND",
            e,
            ref=ref['$ref']
        )

    expected_schema = schema_info.get('$schemaRef')

    if expected_schema is not None:
        if isinstance(expected_schema, basestring):
            if expected_schema != ref_data['$schema']:
                return ValidationError(
                    kind,
                    filename,
                    "INCORRECT_SCHEMA",
                    IncorrectSchema(ref_data['$schema'], expected_schema),
                    ref=ref['$ref']
                )
        else:
            try:
                validator = jsonschema.Draft4Validator(expected_schema)
                validator.validate(ref_data)
            except jsonschema.exceptions.ValidationError as e:
                return ValidationError(kind, filename,
                                       "SCHEMA_REF_VALIDATION_ERROR", e)

    return ValidationRefOK(kind, filename, ref['$ref'], data['$schema'])

def validate_resource_paths(schemas_bundle, bundle, filename, data, ptr, res):
    kind = ValidatedFileKind.RESOURCE_PATH

    valid_json_ext = ('.json',)
    valid_yaml_ext = ('.yml', '.yaml')
    valid_extensions = valid_json_ext + valid_yaml_ext

    logging.info('validating resource path: {}'.format(res['path']))

    file_ext = os.path.splitext(res["path"])
    real_res_path = 'resources' + res['path']

    # Check if file has a valid extension
    if len(file_ext) != 2 or file_ext[1] not in valid_extensions:
        return ValidationError(
            kind,
            filename,
            "MISSING_EXTENSION",
            "The resource file extension should end in one of the following: %s" % ', '.join(valid_extensions),
            res=res['path']
        )

    # Try to open and parse valid yaml
    try:
        with open(real_res_path, 'r') as fh:
            if file_ext[1] in valid_yaml_ext:
                resourcedata = yaml.load(fh, Loader=yamlLoader)
            if file_ext[1] in valid_json_ext:
                resourcedata = json.load(fh)
    except IOError:
        return ValidationError(
            kind,
            filename,
            "FILE_NOT_FOUND",
            "Resource file could not be found: {}".format(real_res_path),
            res=res['path']
        )
    except yaml.YAMLError as e:
        return ValidationError(
            kind,
            filename,
            "INVALID_YAML",
            "Resource file could not be parsed as valid YAML: {}".format(real_res_path),
            res=res['path']
        )
    except json.JSONDecodeError as e:
        return ValidationError(
            kind,
            filename,
            "INVALID_JSON",
            "Resource file could not be parsed as valid JSON: {}".format(real_res_path),
            res=res['path']
        )

    # Ensure our resource has basic fields common to all kubernetes objects
    if not all (k in resourcedata for k in ('apiVersion', 'metadata', 'kind')):
        return ValidationError(
            kind,
            filename,
            "INVALID_OBJECT",
            "Resource file is not a valid kubernetes object: {}".format(real_res_path),
            res=res['path']
        )
    
    # Ensure configmaps and secrets have a data field
    if resourcedata['kind'] in ('ConfigMap', 'Secret'):
        if not 'data' in resourcedata:
            return ValidationError(
                kind,
                filename,
                "INVALID_OBJECT",
                "ConfigMap or Secret resource file is missing a data field: {}".format(real_res_path),
                res=res['path']
            )

    return ValidationRefOK(kind, filename, res['path'], data['$schema'])


def fetch_schema(schema_url):
    if schema_url.startswith('http'):
        r = requests.get(schema_url)
        r.raise_for_status()
        schema = r.text
        return anymarkup.parse(schema, force_types=None)
    else:
        raise MissingSchemaFile(schema_url)


def find_refs(obj, ptr=None, refs=None):
    if refs is None:
        refs = []

    if ptr is None:
        ptr = ""

    if isinstance(obj, dict):
        # is this a ref?
        if '$ref' in obj:
            refs.append((ptr, obj))
        else:
            for key, item in obj.items():
                new_ptr = "{}/{}".format(ptr, key)
                find_refs(item, new_ptr, refs)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            new_ptr = "{}/{}".format(ptr, index)
            find_refs(item, new_ptr, refs)

    return refs

def find_resource_paths(obj, ptr=None, paths=None):
    if paths is None:
        paths = []

    if ptr is None:
        ptr = ""

    if isinstance(obj, dict):
        # is this a ref?
        if 'provider' in obj and obj['provider'] == 'resource' and 'path' in obj:
            paths.append((ptr, obj))
        else:
            for key, item in obj.items():
                new_ptr = "{}/{}".format(ptr, key)
                find_resource_paths(item, new_ptr, paths)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            new_ptr = "{}/{}".format(ptr, index)
            find_resource_paths(item, new_ptr, paths)

    return paths


def get_schema_info_from_pointer(schema, ptr):
    info = schema

    for chunk in ptr.split("/")[1:]:
        if chunk.isdigit():
            info = info['items']
        else:
            info = info['properties'][chunk]

    return info


@click.command()
@click.option('--only-errors', is_flag=True, help='Print only errors')
@click.argument('bundle', type=click.File('rb'))
def main(only_errors, bundle):
    bundle = json.load(bundle)

    data_bundle = bundle['data']
    schemas_bundle = bundle['schemas']

    # Validate schemas
    results_schemas = [
        validate_schema(schemas_bundle, filename, schema_data).dump()
        for filename, schema_data in schemas_bundle.items()
    ]

    # validate datafiles
    results_files = [
        validate_file(schemas_bundle, filename, data).dump()
        for filename, data in data_bundle.items()
    ]

    # validate refs
    results_refs = [
        validate_ref(schemas_bundle, data_bundle,
                     filename, data, ptr, ref).dump()
        for filename, data in data_bundle.items()
        for ptr, ref in find_refs(data)
    ]

    # validate resource paths
    results_res_paths = [
        validate_resource_paths(schemas_bundle, data_bundle,
                                filename, data, ptr, res).dump()
        for filename, data in data_bundle.items()
        for ptr, res in find_resource_paths(data)
    ]

    # Calculate errors
    results = results_schemas + results_files + results_refs + results_res_paths
    errors = list(filter(lambda x: x['result']['status'] == 'ERROR', results))

    # Output
    if only_errors:
        sys.stdout.write(json.dumps(errors, indent=4) + "\n")
    else:
        sys.stdout.write(json.dumps(results, indent=4) + "\n")

    if len(errors) > 0:
        sys.exit(1)
