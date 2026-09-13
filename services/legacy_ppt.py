"""Read current-revision PowerPoint binary text from bounded OLE records.

Record identifiers/layout follow MS-PPT and Apache POI's HSLF record readers.
Only the active Document slide list and its referenced Slide records are read;
old edits, slide masters, notes and document metadata are not body evidence.
"""
import struct


def read_ppt(path, max_chars, max_bytes, preview=False, refine=False):
    import olefile
    with olefile.OleFileIO(path) as ole:
        if not ole.exists('PowerPoint Document') or not ole.exists('Current User'):
            raise ValueError('PPT 缺少正文或当前修订指针')
        if ole.get_size('PowerPoint Document') > max_bytes:
            raise ValueError('PPT 正文流超过本次容器读取预算')
        user = ole.openstream('Current User').read(64)
        data = ole.openstream('PowerPoint Document').read(max_bytes+1)
    if len(user) < 20 or len(data) > max_bytes:
        raise ValueError('PPT 修订指针无效或正文超过预算')

    def u32(offset):
        if offset < 0 or offset+4 > len(data):
            raise ValueError('PPT 记录偏移超出正文')
        return struct.unpack_from('<I',data,offset)[0]

    def record(offset, end=None):
        bound = len(data) if end is None else end
        if offset < 0 or offset+8 > bound:
            raise ValueError('PPT 记录头不完整')
        flags, kind, size = struct.unpack_from('<HHI',data,offset)
        stop = offset+8+size
        if stop > bound:
            raise ValueError('PPT 记录长度超出容器')
        return flags, kind, offset+8, stop

    def records(start,end):
        while start < end:
            value=record(start,end)
            yield value
            start=value[3]

    edit = struct.unpack_from('<I',user,16)[0]
    seen, pointers, document_ref = set(), {}, None
    while edit:
        if edit in seen or len(seen) >= 1024:
            raise ValueError('PPT 修订链循环或超过预算')
        seen.add(edit)
        _, kind, start, end = record(edit)
        if kind != 4085 or end-start < 28:
            raise ValueError('PPT 当前修订记录无效')
        if end-start >= 32 and u32(start+28) not in (0,0xffffffff):
            raise ValueError('PPT 已加密，需要提供解密副本')
        if document_ref is None:
            document_ref=u32(start+16)
        previous, ptr = u32(start+8), u32(start+12)
        _, kind, pos, stop = record(ptr)
        if kind not in (6001,6002):
            raise ValueError('PPT 持久化引用表无效')
        while pos < stop:
            if pos+4 > stop:
                raise ValueError('PPT 持久化引用被截断')
            header=u32(pos);pos+=4
            base,count=header&0xfffff,header>>20
            if not count or pos+4*count > stop:
                raise ValueError('PPT 持久化引用数量无效')
            for i in range(count):
                pointers.setdefault(base+i,u32(pos));pos+=4
        edit=previous
    if document_ref not in pointers:
        raise ValueError('PPT 当前文档引用不存在')
    _,kind,start,end=record(pointers[document_ref])
    if kind != 1000:
        raise ValueError('PPT 当前文档容器无效')

    def text_atom(kind,start,end):
        if kind == 4000:
            if (end-start)%2:
                raise ValueError('PPT Unicode 文本长度无效')
            return data[start:end].decode('utf-16-le','replace').replace('\r','\n')
        if kind == 4008:
            # TextBytesAtom stores the low byte of Unicode characters.
            return data[start:end].decode('latin-1').replace('\r','\n')
        return None

    slides=[]
    for flags,kind,a,b in records(start,end):
        if kind != 4080 or flags>>4 != 0:
            continue
        current=None
        for _,child,a2,b2 in records(a,b):
            if child == 1011:
                if b2-a2 < 16:
                    raise ValueError('PPT 幻灯片引用无效')
                current={'ref':u32(a2),'texts':[]}
                slides.append(current)
            elif current is not None:
                text=text_atom(child,a2,b2)
                if text and text.strip():current['texts'].append(text)
    if not slides:
        raise ValueError('PPT 当前修订未找到幻灯片正文')
    visited=0
    def texts(start,end,depth=0):
        nonlocal visited
        if depth>48:
            raise ValueError('PPT 形状嵌套超过预算')
        for flags,kind,a,b in records(start,end):
            visited+=1
            if visited>1000000:
                raise ValueError('PPT 记录数量超过预算')
            text=text_atom(kind,a,b)
            if text and text.strip():
                yield text
            elif flags&15 == 15 or kind in (1036,0xf00d):
                yield from texts(a,b,depth+1)

    indices=list(range(len(slides)))
    truncated=False
    if preview and len(indices)>(6 if refine else 3):
        n=6 if refine else 3
        indices=sorted({round(i*(len(slides)-1)/(n-1)) for i in range(n)})
        truncated=True
    parts,sections=[],[]
    length=0
    visible_characters=0
    for index in indices:
        slide=slides[index]
        values=list(slide['texts'])
        if slide['ref'] not in pointers:
            raise ValueError('PPT 幻灯片正文引用缺失')
        _,kind,a,b=record(pointers[slide['ref']])
        if kind!=1006:
            raise ValueError('PPT 幻灯片引用指向了其他类型')
        values.extend(texts(a,b))
        # Text placeholders can also be embedded in the shape record.
        unique=list(dict.fromkeys(value.strip() for value in values if value.strip()))
        text='\n'.join(unique)
        visible_characters+=len(text.strip())
        location='幻灯片 {}'.format(index+1)
        parts.append('[{}]\n{}'.format(location,text))
        if preview:
            for offset in range(0,min(len(text),6000),1500):
                if len(sections)<48:sections.append({'location':location+'，正文字符 '+str(offset+1),'text':text[offset:offset+1500],'kind':'text'})
        length+=len(parts[-1])+2
        if length>=max_chars:
            truncated=True
            break
    if not visible_characters:
        raise ValueError('PPT 幻灯片未获得可读正文；不能把页码标记当作正文')
    return '\n\n'.join(parts), {'slide_count':len(slides),'sections':sections,
        'current_revision_only':True,'content_complete':not truncated,'embedded_objects_ocr':False}, truncated
