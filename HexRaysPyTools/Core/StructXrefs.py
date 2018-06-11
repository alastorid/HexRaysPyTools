import time
import logging
from collections import namedtuple
import json

import idaapi
import Helper
import HexRaysPyTools.Settings as Settings


logger = logging.getLogger(__name__)

XrefInfo = namedtuple('XrefInfo', ['func_ea', 'offset', 'line', 'type'])


def singleton(cls):
    instances = {}

    def get_instance():
        if cls not in instances:
            instances[cls] = cls()
        return instances[cls]
    return get_instance


@singleton
class XrefStorage(object):
    ARRAY_NAME = "$HexRaysPyTools:XrefStorage"

    def __init__(self):
        self.storage = None

    def open(self):
        if not Settings.STORE_XREFS:
            self.storage = {}
            return

        result = Helper.load_long_str_from_idb(self.ARRAY_NAME)
        if result:
            try:
                self.storage = json.loads(result, object_hook=self.json_keys_to_str)
                return
            except ValueError:
                logger.error("Failed to read previous info about Xrefs. Try Ctrl+F5 to cache data")
        self.storage = {}

    def close(self):
        self.save()
        self.storage = None

    def save(self):
        if not Settings.STORE_XREFS:
            return

        if self.storage:
            Helper.save_long_str_to_idb(self.ARRAY_NAME, json.dumps(self.storage))

    def update_structure_info(self, ordinal, function_address, data):
        """ Accepts data in form dictionary {structure offset -> list(offsets within function with field appealing) """
        if ordinal not in self.storage:
            self.storage[ordinal] = {}

        image_base = idaapi.get_imagebase()
        function_offset = function_address - image_base
        self.storage[ordinal][function_offset] = data

    def get_structure_info(self, ordinal, struct_offset):
        """ By given ordinal and offset within a structure returns dictionary {func_address -> list(offsets)} """
        result = []

        if ordinal not in self.storage:
            return result

        for func_offset, data in self.storage[ordinal].items():
            if struct_offset in data:
                func_ea = func_offset + idaapi.get_imagebase()
                for xref_info in data[struct_offset]:
                    offset, line, usage_type = xref_info
                    result.append(XrefInfo(func_ea, offset, line, usage_type))
        return result

    @staticmethod
    def json_keys_to_str(x):
        if isinstance(x, dict):
            return {int(k): v for k, v in x.items()}
        return x

    def __len__(self):
        return len(str(self.storage))


class StructXrefVisitor(idaapi.ctree_parentee_t):
    def __init__(self, cfunc):
        super(StructXrefVisitor, self).__init__()
        self.__cfunc = cfunc
        self.__image_base = idaapi.get_imagebase()
        self.__function_address = cfunc.entry_ea
        self.__result = {}
        self.__storage = XrefStorage()

    def visit_expr(self, expression):
        # Checks if expression is reference by pointer or by value
        if expression.op == idaapi.cot_memptr:
            struct_type = expression.x.type.get_pointed_object()
        elif expression.op == idaapi.cot_memref:
            struct_type = expression.x.type
        else:
            return 0

        # Getting information about structure, field offset, address and one line corresponding to code
        ordinal = struct_type.get_ordinal()
        if ordinal == 0:
            t = idaapi.tinfo_t()
            struct_name = struct_type.dstr().split()[-1]        # Get rid of `struct` prefix or something else
            t.get_named_type(idaapi.cvar.idati, struct_name)
            ordinal = t.get_ordinal()

        field_offset = expression.m
        ea = self.__find_ref_address(expression)
        usage_type = self.__get_type(expression)

        if ea == idaapi.BADADDR or not ordinal:
            logger.warning("Failed to parse at address {0}, ordinal - {1}, type - {2}".format(
                Helper.to_hex(ea), ordinal, struct_type.dstr()
            ))

        one_line = self.__get_line()

        occurrence_offset = ea - self.__function_address
        xref_info = (occurrence_offset, one_line, usage_type)

        # Saving results
        if ordinal not in self.__result:
            self.__result[ordinal] = {field_offset: [xref_info]}
        elif field_offset not in self.__result[ordinal]:
            self.__result[ordinal][field_offset] = [xref_info]
        else:
            self.__result[ordinal][field_offset].append(xref_info)
        return 0

    def process(self):
        t = time.time()
        self.apply_to(self.__cfunc.body, None)
        for ordinal, data in self.__result.items():
            self.__storage.update_structure_info(ordinal, self.__function_address, data)

        storage_mb_size = len(self.__storage) * 1.0 / 1024 ** 2
        logger.debug("Xref processing: %f seconds passed, storage size - %.2f MB ", (time.time() - t), storage_mb_size)

    def __find_ref_address(self, cexpr):
        """ Returns most close virtual address corresponding to cexpr """

        ea = cexpr.ea
        if ea != idaapi.BADADDR:
            return ea

        for p in reversed(self.parents):
            if p.ea != idaapi.BADADDR:
                return p.ea

    def __get_type(self, cexpr):
        """ Returns one of the following types: 'R' - read value, 'W' - write value, 'A' - function argument"""
        child = cexpr
        for p in reversed(self.parents):
            assert p, "Failed to get type at " + Helper.to_hex(self.__function_address)

            if p.cexpr.op == idaapi.cot_call:
                return 'Arg'
            if not p.is_expr():
                return 'R'
            if p.cexpr.op == idaapi.cot_asg:
                if p.cexpr.x == child:
                    return 'W'
                return 'R'
            child = p.cexpr

    def __get_line(self):
        for p in reversed(self.parents):
            if not p.is_expr():
                return idaapi.tag_remove(p.print1(self.__cfunc))
        AssertionError("Parent instruction is not found")
