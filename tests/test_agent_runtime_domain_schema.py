"""Domain schemas constrain generation without changing existing callers."""
import pytest
from services.agent_runtime import _vllm_structured_response_format,PydanticAgentRuntime


def test_nested_domain_schema_is_preserved_and_copied():
    schema={'type':'object','properties':{'findings':{'type':'array','items':{'type':'object','properties':{'kind':{'enum':['received','goal']},'conditions':{'type':'array','items':{'type':'string'}}}}}},'required':['findings']}
    result=_vllm_structured_response_format(('findings',),schema)
    assert result['json_schema']['schema']==schema
    result['json_schema']['schema']['properties']['findings']['items']['properties']['kind']['enum'].append('unsafe')
    assert schema['properties']['findings']['items']['properties']['kind']['enum']==['received','goal']


def test_domain_schema_cannot_remove_required_top_level_contract():
    with pytest.raises(ValueError):_vllm_structured_response_format(('findings',),{'type':'object','required':[]})
    with pytest.raises(ValueError):_vllm_structured_response_format(('findings',),{'type':'array'})


def test_runtime_sends_nested_schema_to_supported_transport():
    class Transport:
        model='fixture';base_url='fixture://schema';configured=True;requires_confirmation=False;privacy_label='fixture'
        supports_json_response_format=True
        def chat(self,system,prompt,**kwargs):
            self.kwargs=kwargs
            return {'content':'{"findings":[]}','finish_reason':'stop','usage':{}}
    schema={'type':'object','properties':{'findings':{'type':'array','items':{'type':'object','properties':{'conditions':{'type':'array','items':{'type':'string'}}}}}},'required':['findings']}
    transport=Transport()
    result=PydanticAgentRuntime(transport).chat_json('fixture','fixture',required_fields=('findings',),output_schema=schema)
    assert result['json']=={'findings':[]}
    assert transport.kwargs['response_format']['json_schema']['schema']==schema
    assert 'output_schema' not in transport.kwargs
