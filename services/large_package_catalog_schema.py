"""Bounded JSON output contracts; semantic evidence validation still runs afterwards."""
def object_schema(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}


def leaf(material,key=None):
    ids=[e['id'] for e in material['evidence']]
    refs={'type':'array','items':{'type':'string','enum':ids},'maxItems':len(ids)}
    props={
        'name':{'type':'string','minLength':4,'maxLength':80},
        'description':{'type':'string','minLength':8,'maxLength':260},
        'evidence_ids':dict(refs,minItems=1),
        'needs_review':{'type':'boolean'},
        'purity':{'type':'string','enum':['same','mixed','unsure']},
        'outliers':refs,
        'subtopics':{'type':'array','maxItems':min(8,len(ids)), 'items':object_schema({
            'name':{'type':'string','minLength':4,'maxLength':24},'members':dict(refs,minItems=1)})}}
    if key is not None:props={'id':{'type':'string','enum':[key]},**props}
    from services.large_package_catalog_naming import compact_output_enabled
    required = None
    if compact_output_enabled():
        # Compact optional fields match the prompt and parser defaults. Purity,
        # evidence references and the common range are always mandatory.
        required = (['id'] if key is not None else []) + ['name','description','evidence_ids','purity']
    return object_schema(props, required)


def response(material=None,pending=None):
    schema=leaf(material) if pending is None else object_schema({'groups':{'type':'array',
         'minItems':len(pending),'maxItems':len(pending),
         'items':{'anyOf':[leaf(m,k) for _,m,k in pending]}}})
    from services.large_package_catalog_naming import compact_output_enabled
    # OpenAI strict schemas require all properties. Optional compact diagnostics
    # use ordinary JSON-schema validation; semantic validation still runs.
    return {'type':'json_schema','json_schema':{'name':'catalog_names','strict':not compact_output_enabled(),'schema':schema}}


def themes(keys):
    return {'type':'json_schema','json_schema':{'name':'catalog_themes','strict':True,
         'schema':object_schema({k:{'type':'string','minLength':4,'maxLength':32} for k in keys})}}


def output_budget(cfg,material):
    base=max(cfg['output'],cfg.get('namer_output',cfg['output']))
    return max(base,600) if material.get('route')=='boundary' else base
