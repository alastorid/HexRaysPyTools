import ida_frame
import ida_struct
import idaapi
import idautils
import idc
import Const

import collections
import re

temporary_structure = None
demangled_names = {}
potential_negatives = {}
name_regex = re.compile(r"^a[\d]*[a]?$")

def init_demangled_names(*args):
    """
    Creates dictionary of demangled names => address, that will be used further at double click on methods got from
    symbols.
    """
    demangled_names.clear()
    for address, name in idautils.Names():
        short_name = idc.Demangle(name, idc.GetLongPrm(idc.INF_SHORT_DN))
        if short_name:
            demangled_names[short_name.split('(')[0]] = address - idaapi.get_imagebase()

            # Names can have templates and should be transformed before creating local type
            name = re.sub(r'[<>]', '_t_', name)

            # Thunk functions with name like "[thunk]:CWarmupHostProvider::Release`adjustor{8}'"
            result = re.search(r"(\[thunk\]:)?([^`]*)(.*\{(\d+)}.*)?", short_name)
            name, adjustor = result.group(2), result.group(4)
            if adjustor:
                demangled_names[name + "_adj_" + adjustor] = address - idaapi.get_imagebase()

    print "[DEBUG] Demangled names have been initialized"

def is_func_call(item,cfunc):
    if type(item) is idaapi.ctree_item_t:
        item = item.it
    while True:
        parent = cfunc.body.find_parent_of(item)
        if not parent or not parent.is_expr() or parent.to_specific_type.x.index != item.index:
            return False
        elif parent.op == idaapi.cot_call:
            return True
        item = parent

def is_func_arg(item,cfunc):
    if type(item) is idaapi.ctree_item_t:
        item = item.it
    while True:
        parent = cfunc.body.find_parent_of(item)
        if not parent or not parent.is_expr():
            return False
        elif parent.op == idaapi.cot_call and parent.to_specific_type.x.index != item.index:
            return True
        item = parent

def get_nodes_to_call_parent(item,cfunc):
    if type(item) is idaapi.ctree_item_t:
        item = item.it
    rc = [item]
    while True:
        parent = cfunc.body.find_parent_of(item)
        if not parent or not parent.is_expr():
            return None
        rc.append(parent)
        if parent.op == idaapi.cot_call:
            rc.reverse()
            return rc
        item = parent

def get_up_branch_to_op(cfunc,item,ops):
    if type(item) is idaapi.ctree_item_t:
        item = item.it
    rc = [item]
    while True:
        parent = cfunc.body.find_parent_of(item)
        if not parent or not parent.is_expr():
            return None
        rc.append(parent)
        if parent.op in ops:
            rc.reverse()
            return rc
        item = parent

def get_branch(cfunc,item):
    if type(item) is idaapi.ctree_item_t:
        item = item.it
    rc = [item.cexpr]
    while True:
        parent = cfunc.body.find_parent_of(item)
        if not parent or not parent.is_expr():
            break
        rc.append(parent.cexpr)
        item = parent
    rc.reverse()
    return rc

def get_virtual_func_address(name, tinfo=None, offset=None):
    """
    :param name: method name
    :param tinfo: class tinfo
    :param offset: virtual table offset
    :return: address of the method
    """
    # address = idaapi.BADADDR
    # if name in idautils.Functions():
    address = idc.LocByName(name) #fucking idaapi. It's function work incorrectly. This returned 0xFF005BE7 instead BADADDR in some cases. get_name_ea problem.

    if address != idaapi.BADADDR:
        return address

    address = demangled_names.get(name, idaapi.BADADDR)
    if address != idaapi.BADADDR:
        return address + idaapi.get_imagebase()

    if tinfo is None or offset is None:
        return

    offset *= 8
    udt_member = idaapi.udt_member_t()
    while tinfo.is_struct():
        address = demangled_names.get(tinfo.dstr() + '::' + name, idaapi.BADADDR)
        if address != idaapi.BADADDR:
            return address + idaapi.get_imagebase()
        udt_member.offset = offset
        tinfo.find_udt_member(idaapi.STRMEM_OFFSET, udt_member)
        tinfo = udt_member.type
        offset = offset - udt_member.offset


def get_func_argument_info(function, expression):
    """
    Function is cexpr with opname == 'cot_call', expression is any son. Returns index of argument and it's type

    :param function: idaapi.cexpr_t
    :param expression: idaapi.cexpr_t
    :return: (int, idaapi.tinfo_t)
    """
    for idx, argument in enumerate(function.a):
        if expression == argument.cexpr:
            return idx, function.x.type.get_nth_arg(idx)
    print "[ERROR] Wrong usage of 'Helper.get_func_argument_info()'"


def get_nice_pointed_object(tinfo):
    """
    Returns nice pointer name (if exist) or None.
    For example if tinfo is PKSPIN_LOCK which is typedef of unsigned int *, then if in local types exist KSPIN_LOCK with
    type unsigned int, this function returns KSPIN_LOCK
    """
    try:
        name = tinfo.dstr()
        if name[0] == 'P':
            pointed_tinfo = idaapi.tinfo_t()
            if pointed_tinfo.get_named_type(idaapi.cvar.idati, name[1:]):
                if tinfo.get_pointed_object().equals_to(pointed_tinfo):
                    return pointed_tinfo
    except TypeError:
        pass


def get_fields_at_offset(tinfo, offset):
    """
    Given tinfo and offset of the structure or union, returns list of all tinfo at that offset.
    This function helps to find appropriate structures by type of the offset
    """
    result = []
    if offset == 0:
        result.append(tinfo)
    udt_data = idaapi.udt_type_data_t()
    tinfo.get_udt_details(udt_data)
    udt_member = idaapi.udt_member_t()
    udt_member.offset = offset * 8
    idx = tinfo.find_udt_member(idaapi.STRMEM_OFFSET, udt_member)
    if idx != -1:
        while idx < tinfo.get_udt_nmembers() and udt_data[idx].offset <= offset * 8:
            udt_member = udt_data[idx]
            if udt_member.offset == offset * 8:
                if udt_member.type.is_ptr():
                    result.append(idaapi.get_unk_type(Const.EA_SIZE))
                    result.append(udt_member.type)
                    result.append(idaapi.dummy_ptrtype(Const.EA_SIZE, False))
                elif not udt_member.type.is_udt():
                    result.append(udt_member.type)
            if udt_member.type.is_array():
                if (offset - udt_member.offset / 8) % udt_member.type.get_array_element().get_size() == 0:
                    result.append(udt_member.type.get_array_element())
            elif udt_member.type.is_udt():
                result.extend(get_fields_at_offset(udt_member.type, offset - udt_member.offset / 8))
            idx += 1
    return result


def is_legal_type(tinfo):
    tinfo.clr_const()
    if tinfo.is_ptr() and tinfo.get_pointed_object().is_forward_decl():
        return tinfo.get_pointed_object().get_size() == idaapi.BADSIZE
    return bool(filter(lambda x: x.equals_to(tinfo), Const.LEGAL_TYPES))


def search_duplicate_fields(udt_data):
    # Returns list of lists with duplicate fields

    default_dict = collections.defaultdict(list)
    for idx, udt_member in enumerate(udt_data):
        default_dict[udt_member.name].append(idx)
    return [indices for indices in default_dict.values() if len(indices) > 1]

def is_gap(structure_name,field_offset):
    sid = idaapi.get_struc_id(structure_name)
    if sid != idaapi.BADADDR:
        sptr = idaapi.get_struc(sid)
        mptr = idaapi.get_member(sptr, field_offset)
        if mptr:
            return False
        else:
            return True


def get_struct_member_type(structure_name, field_offset):
    sid = idaapi.get_struc_id(structure_name)
    if sid != idaapi.BADADDR:
        sptr = idaapi.get_struc(sid)
        mptr = idaapi.get_member(sptr, field_offset)
        if mptr:
            tif = idaapi.tinfo_t()
            idaapi.get_member_tinfo2(mptr, tif)
            return tif
        return None


touched_functions = set()


class FunctionTouchVisitor(idaapi.ctree_parentee_t):
    def __init__(self, cfunc):
        super(FunctionTouchVisitor, self).__init__()
        self.functions = set()
        self.cfunc = cfunc

    def visit_expr(self, expression):
        if expression.op == idaapi.cot_call:
            self.functions.add(expression.x.obj_ea)
        return 0

    def touch_all(self):
        for address in self.functions.difference(touched_functions):
            touched_functions.add(address)
            try:
                cfunc = idaapi.decompile(address)
                if cfunc:
                    FunctionTouchVisitor(cfunc).process()
            except idaapi.DecompilationFailure:
                print "[ERROR] IDA failed to decompile function at 0x{address:08X}".format(address=address)
        idaapi.decompile(self.cfunc.entry_ea)

    def process(self):
        if self.cfunc.entry_ea not in touched_functions:
            touched_functions.add(self.cfunc.entry_ea)
            self.apply_to(self.cfunc.body, None)
            self.touch_all()
            return True
        return False


def fix_automatic_naming(func_data):

    for i in range(0, len(func_data)):
        if name_regex.match(func_data[i].name):
            func_data[i].name = ""




    # fn_ea = vu.cfunc.entry_ea
    # fn = idaapi.get_func(fn_ea)
    # frame = idaapi.get_frame(fn)
    # end_off = idaapi.frame_off_savregs(fn)
    #
    # for i in range(0, frame.memqty):
    #     member = frame.get_member(i)
    #     off = member.soff
    #     if off >= end_off:
    #         break
    #     name = idaapi.get_member_name(member.id)
    #     if name_regex.match(name):
    #         ida_struct.set_member_name(frame, off, ida_frame.build_stkvar_name(fn, off))