import sys, os, struct, json, re

ACC_FLAGS = [
    (0x0001, "public"), (0x0002, "private"), (0x0004, "protected"), (0x0008, "static"),
    (0x0010, "final"), (0x0020, "super/sync"), (0x0040, "volatile/bridge"), (0x0080, "transient/varargs"),
    (0x0200, "interface"), (0x0400, "abstract"), (0x1000, "synthetic"), (0x2000, "annotation"),
    (0x4000, "enum"),
]

def flags_str(flags, is_class=False):
    out = []
    if flags & 0x0001: out.append("public")
    if flags & 0x0002: out.append("private")
    if flags & 0x0004: out.append("protected")
    if flags & 0x0008: out.append("static")
    if flags & 0x0010: out.append("final")
    if is_class and flags & 0x0200: out.append("interface")
    if flags & 0x0400: out.append("abstract")
    if flags & 0x1000: out.append("synthetic")
    return out

class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
    def u1(self):
        v = self.data[self.pos]; self.pos += 1; return v
    def u2(self):
        v = struct.unpack_from(">H", self.data, self.pos)[0]; self.pos += 2; return v
    def u4(self):
        v = struct.unpack_from(">I", self.data, self.pos)[0]; self.pos += 4; return v
    def skip(self, n):
        self.pos += n
    def bytes(self, n):
        v = self.data[self.pos:self.pos+n]; self.pos += n; return v

def parse_class(data, fname):
    r = Reader(data)
    magic = r.u4()
    if magic != 0xCAFEBABE:
        return {"error": "bad magic", "file": fname}
    minor = r.u2(); major = r.u2()
    cp_count = r.u2()
    cp = {}  # index -> parsed entry dict
    i = 1
    while i < cp_count:
        tag = r.u1()
        if tag == 1:  # Utf8
            length = r.u2()
            b = r.bytes(length)
            if len(b) > 0 and b[-1] == 0:
                b = b[:-1]
            try:
                s = b.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    s = b.decode("euc_jp")
                except UnicodeDecodeError:
                    s = b.decode("latin-1", errors="replace")
            cp[i] = ("Utf8", s)
        elif tag == 7:  # Class
            name_idx = r.u2()
            cp[i] = ("Class", name_idx)
        elif tag == 8:  # String
            str_idx = r.u2()
            cp[i] = ("String", str_idx)
        elif tag in (9, 10, 11):  # Fieldref, Methodref, IfaceMethodref
            c = r.u2(); nt = r.u2()
            cp[i] = ({9:"Fieldref",10:"Methodref",11:"IfaceMethodref"}[tag], c, nt)
        elif tag == 3:  # Integer
            v = struct.unpack_from(">i", data, r.pos)[0]; r.skip(4)
            cp[i] = ("Integer", v)
        elif tag == 4:  # Float
            v = struct.unpack_from(">f", data, r.pos)[0]; r.skip(4)
            cp[i] = ("Float", v)
        elif tag == 5:  # Long
            v = struct.unpack_from(">q", data, r.pos)[0]; r.skip(8)
            cp[i] = ("Long", v)
            i += 1
        elif tag == 6:  # Double
            v = struct.unpack_from(">d", data, r.pos)[0]; r.skip(8)
            cp[i] = ("Double", v)
            i += 1
        elif tag == 12:  # NameAndType
            n = r.u2(); d = r.u2()
            cp[i] = ("NameAndType", n, d)
        elif tag == 15:  # MethodHandle
            rk = r.u1(); ri = r.u2()
            cp[i] = ("MethodHandle", rk, ri)
        elif tag == 16:  # MethodType
            d = r.u2()
            cp[i] = ("MethodType", d)
        elif tag in (17, 18):  # Dynamic, InvokeDynamic
            bm = r.u2(); nt = r.u2()
            cp[i] = ("Dynamic", bm, nt)
        elif tag in (19, 20):  # Module, Package
            n = r.u2()
            cp[i] = ("ModPkg", n)
        else:
            raise ValueError(f"{fname}: unknown cp tag {tag} at {r.pos}, index {i}/{cp_count}")
        i += 1

    def utf(idx):
        e = cp.get(idx)
        if e and e[0] == "Utf8":
            return e[1]
        return None

    def classname(idx):
        e = cp.get(idx)
        if e and e[0] == "Class":
            return utf(e[1])
        return None

    def nat(idx):
        e = cp.get(idx)
        if e and e[0] == "NameAndType":
            return utf(e[1]), utf(e[2])
        return None, None

    access = r.u2()
    this_class = classname(r.u2())
    super_idx = r.u2()
    super_class = classname(super_idx) if super_idx else None
    iface_count = r.u2()
    interfaces = [classname(r.u2()) for _ in range(iface_count)]

    def skip_attributes(n):
        attrs = []
        for _ in range(n):
            name_idx = r.u2()
            name = utf(name_idx)
            length = r.u4()
            body = r.bytes(length)
            attrs.append((name, body))
        return attrs

    fields = []
    field_count = r.u2()
    for _ in range(field_count):
        facc = r.u2(); fname_idx = r.u2(); fdesc_idx = r.u2()
        fattr_count = r.u2()
        fattrs = skip_attributes(fattr_count)
        const_val = None
        for aname, abody in fattrs:
            if aname == "ConstantValue" and len(abody) >= 2:
                cv_idx = struct.unpack_from(">H", abody, 0)[0]
                e = cp.get(cv_idx)
                if e:
                    const_val = e[1] if e[0] != "String" else utf(e[1])
        fields.append({
            "name": utf(fname_idx), "descriptor": utf(fdesc_idx),
            "flags": flags_str(facc), "const_value": const_val,
        })

    methods = []
    method_count = r.u2()
    for _ in range(method_count):
        macc = r.u2(); mname_idx = r.u2(); mdesc_idx = r.u2()
        mattr_count = r.u2()
        mattrs = skip_attributes(mattr_count)
        code_len = None
        for aname, abody in mattrs:
            if aname == "Code":
                code_len = len(abody)
        methods.append({
            "name": utf(mname_idx), "descriptor": utf(mdesc_idx),
            "flags": flags_str(macc), "code_bytes": code_len,
        })

    class_attr_count = r.u2()
    class_attrs = skip_attributes(class_attr_count)
    source_file = None
    inner_classes = []
    for aname, abody in class_attrs:
        if aname == "SourceFile" and len(abody) >= 2:
            sf_idx = struct.unpack_from(">H", abody, 0)[0]
            source_file = utf(sf_idx)
        elif aname == "InnerClasses":
            cnt = struct.unpack_from(">H", abody, 0)[0]
            off = 2
            for _ in range(cnt):
                inner_ci, outer_ci, name_i, iflags = struct.unpack_from(">HHHH", abody, off)
                off += 8
                inner_classes.append({
                    "inner": classname(inner_ci) if inner_ci else None,
                    "outer": classname(outer_ci) if outer_ci else None,
                    "name": utf(name_i) if name_i else None,
                })

    # gather all distinct referenced classes (via Class constants) excluding self
    referenced_classes = set()
    for e in cp.values():
        if e[0] == "Class":
            n = utf(e[1])
            if n and n != this_class:
                referenced_classes.add(n)

    # gather string literals (CONSTANT_String) actually used
    string_literals = set()
    for e in cp.values():
        if e[0] == "String":
            s = utf(e[1])
            if s:
                string_literals.add(s)

    # gather field/method refs (owner.name:descriptor) for call/access graph
    member_refs = set()
    for e in cp.values():
        if e[0] in ("Fieldref", "Methodref", "IfaceMethodref"):
            owner = classname(e[1])
            n, d = nat(e[2])
            if owner and n:
                member_refs.add(f"{owner}.{n}")

    return {
        "file": fname,
        "class_name": this_class,
        "super_class": super_class,
        "interfaces": interfaces,
        "flags": flags_str(access, is_class=True),
        "source_file": source_file,
        "fields": fields,
        "methods": methods,
        "inner_classes": inner_classes,
        "referenced_classes": sorted(referenced_classes),
        "string_literals": sorted(string_literals),
        "member_refs": sorted(member_refs),
        "minor": minor, "major": major,
        "size": len(data),
    }

def main():
    indir = sys.argv[1]
    outpath = sys.argv[2]
    results = []
    errors = []
    files = sorted(os.listdir(indir))
    for fn in files:
        if not fn.endswith(".class"):
            continue
        path = os.path.join(indir, fn)
        with open(path, "rb") as f:
            data = f.read()
        try:
            parsed = parse_class(data, fn)
            results.append(parsed)
        except Exception as e:
            errors.append({"file": fn, "error": str(e)})
    with open(outpath, "w") as f:
        json.dump({"classes": results, "errors": errors, "total_files": len(files)}, f)
    print(f"parsed {len(results)} ok, {len(errors)} errors, total {len(files)}")

if __name__ == "__main__":
    main()
