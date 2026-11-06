from types import SimpleNamespace
import pytest
from utils.request_boundary import is_verified_conversion_return


@pytest.mark.parametrize('change', [None,'host','lookalike','http','port','userinfo','source_path',
                                   'target_path','target_host','status307','method','navigation','main_frame'])
def test_conversion_exception_is_exact_and_read_only(change):
    request=SimpleNamespace(url='https://www.google.com/measurement/conversion?opaque=SECRET',
        method='POST',resource_type='fetch',is_navigation_request=lambda:False,
        frame=SimpleNamespace(parent_frame=object()))
    target='https://www.google.com/measurement/1p-conversion/?opaque=SECRET'
    status=302
    if change=='host':request.url=request.url.replace('www.google.com','untrusted.invalid')
    if change=='lookalike':request.url=request.url.replace('www.google.com','www.google.com.untrusted.invalid')
    if change=='http':request.url=request.url.replace('https:','http:')
    if change=='port':request.url=request.url.replace('www.google.com','www.google.com:444')
    if change=='userinfo':request.url=request.url.replace('//www','//user@www')
    if change=='source_path':request.url=request.url.replace('/measurement/conversion','/account/login')
    if change=='target_path':target=target.replace('/measurement/1p-conversion/','/cart/add.js')
    if change=='target_host':target=target.replace('www.google.com','untrusted.invalid')
    if change=='status307':status=307
    if change=='method':request.method='PUT'
    if change=='navigation':request.is_navigation_request=lambda:True
    if change=='main_frame':request.frame.parent_frame=None
    assert is_verified_conversion_return(request,status,target) is (change is None)


@pytest.mark.parametrize('target,allowed', [
    ('https://www.google.com/pagead/1p-conversion/123456789/?opaque=SECRET',True),
    ('https://www.google.com/pagead/1p-conversion/987654321/',False),
    ('https://untrusted.invalid/pagead/1p-conversion/123456789/',False),
    ('https://www.google.com/cart/add.js',False),
    ('http://www.google.com/pagead/1p-conversion/123456789/',False),
])
def test_pagead_return_requires_the_same_ad_id_and_exact_google_endpoint(target,allowed):
    request=SimpleNamespace(url='https://googleads.g.doubleclick.net/pagead/viewthroughconversion/123456789/?opaque=SECRET',
        method='POST',resource_type='fetch',is_navigation_request=lambda:False,
        frame=SimpleNamespace(parent_frame=object()))
    assert is_verified_conversion_return(request,302,target) is allowed
