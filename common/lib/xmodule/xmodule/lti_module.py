"""
Learning Tools Interoperability (LTI) module.


Resources
---------

Theoretical background and detailed specifications of LTI can be found on:

    http://www.imsglobal.org/LTI/v1p1p1/ltiIMGv1p1p1.html

This module is based on the version 1.1.1 of the LTI specifications by the
IMS Global authority. For authentication, it uses OAuth1.

When responding back to the LTI tool provider, we must issue a correct
response. Types of responses and their message payload is available at:

    Table A1.2 Interpretation of the 'CodeMajor/severity' matrix.
    http://www.imsglobal.org/gws/gwsv1p0/imsgws_wsdlBindv1p0.html

A resource to test the LTI protocol (PHP realization):

    http://www.imsglobal.org/developers/LTI/test/v1p1/lms.php


What is supported:
------------------

1.) Display of simple LTI in iframe or a new window.
2.) Multiple LTI components on a single page.
3.) The use of multiple LTI providers per course.
4.) Use of advanced LTI component that provides back a grade.
    a.) The LTI provider sends back a grade to a specified URL.
    b.) Currently only action "update" is supported. "Read", and "delete"
        actions initially weren't required.
"""

import logging
import oauthlib.oauth1
from oauthlib.oauth1.rfc5849 import signature
import hashlib
import base64
import urllib
import textwrap
import json
from lxml import etree
from webob import Response
import mock
from xml.sax.saxutils import escape

from xmodule.editing_module import MetadataOnlyEditingDescriptor
from xmodule.raw_module import EmptyDataRawDescriptor
from xmodule.x_module import XModule, module_attr
from xmodule.course_module import CourseDescriptor
from pkg_resources import resource_string
from xblock.core import String, Scope, List, XBlock
from xblock.fields import Boolean, Float


log = logging.getLogger(__name__)


class LTIError(Exception):
    pass


class LTIFields(object):
    """
    Fields to define and obtain LTI tool from provider are set here,
    except credentials, which should be set in course settings::

    `lti_id` is id to connect tool with credentials in course settings. It should not contain :: (double semicolon)
    `launch_url` is launch URL of tool.
    `custom_parameters` are additional parameters to navigate to proper book and book page.

    For example, for Vitalsource provider, `launch_url` should be
    *https://bc-staging.vitalsource.com/books/book*,
    and to get to proper book and book page, you should set custom parameters as::

        vbid=put_book_id_here
        book_location=page/put_page_number_here

    Default non-empty URL for `launch_url` is needed due to oauthlib demand (URL scheme should be presented)::

    https://github.com/idan/oauthlib/blob/master/oauthlib/oauth1/rfc5849/signature.py#L136
    """
    display_name = String(display_name="Display Name", help="Display name for this module", scope=Scope.settings, default="LTI")
    lti_id = String(help="Id of the tool", default='', scope=Scope.settings)
    launch_url = String(help="URL of the tool", default='http://www.example.com', scope=Scope.settings)
    custom_parameters = List(help="Custom parameters (vbid, book_location, etc..)", scope=Scope.settings)
    open_in_a_new_page = Boolean(help="Should LTI be opened in new page?", default=True, scope=Scope.settings)
    graded = Boolean(help="Grades will be considered in overall score.", default=False, scope=Scope.settings)
    weight = Float(
        help="Weight for student grades.",
        default=1.0,
        scope=Scope.settings,
        values={"min": 0},
    )
    has_score = Boolean(help="Does this LTI module have score?", default=False, scope=Scope.settings)


class LTIModule(LTIFields, XModule):
    """
    Module provides LTI integration to course.

    Except usual Xmodule structure it proceeds with OAuth signing.
    How it works::

    1. Get credentials from course settings.

    2.  There is minimal set of parameters need to be signed (presented for Vitalsource)::

            user_id
            oauth_callback
            lis_outcome_service_url
            lis_result_sourcedid
            launch_presentation_return_url
            lti_message_type
            lti_version
            role
            *+ all custom parameters*

        These parameters should be encoded and signed by *OAuth1* together with
        `launch_url` and *POST* request type.

    3. Signing proceeds with client key/secret pair obtained from course settings.
        That pair should be obtained from LTI provider and set into course settings by course author.
        After that signature and other OAuth data are generated.

        OAuth data which is generated after signing is usual::

            oauth_callback
            oauth_nonce
            oauth_consumer_key
            oauth_signature_method
            oauth_timestamp
            oauth_version


    4. All that data is passed to form and sent to LTI provider server by browser via
        autosubmit via JavaScript.

        Form example::

            <form
                action="${launch_url}"
                name="ltiLaunchForm-${element_id}"
                class="ltiLaunchForm"
                method="post"
                target="ltiLaunchFrame-${element_id}"
                encType="application/x-www-form-urlencoded"
            >
                <input name="launch_presentation_return_url" value="" />
                <input name="lis_outcome_service_url" value="" />
                <input name="lis_result_sourcedid" value="" />
                <input name="lti_message_type" value="basic-lti-launch-request" />
                <input name="lti_version" value="LTI-1p0" />
                <input name="oauth_callback" value="about:blank" />
                <input name="oauth_consumer_key" value="${oauth_consumer_key}" />
                <input name="oauth_nonce" value="${oauth_nonce}" />
                <input name="oauth_signature_method" value="HMAC-SHA1" />
                <input name="oauth_timestamp" value="${oauth_timestamp}" />
                <input name="oauth_version" value="1.0" />
                <input name="user_id" value="${user_id}" />
                <input name="role" value="student" />
                <input name="oauth_signature" value="${oauth_signature}" />

                <input name="custom_1" value="${custom_param_1_value}" />
                <input name="custom_2" value="${custom_param_2_value}" />
                <input name="custom_..." value="${custom_param_..._value}" />

                <input type="submit" value="Press to Launch" />
            </form>

    5. LTI provider has same secret key and it signs data string via *OAuth1* and compares signatures.

        If signatures are correct, LTI provider redirects iframe source to LTI tool web page,
        and LTI tool is rendered to iframe inside course.

        Otherwise error message from LTI provider is generated.
    """

    css = {'scss': [resource_string(__name__, 'css/lti/lti.scss')]}

    def get_input_fields(self):
        # LTI provides a list of default parameters that might be passed as
        # part of the POST data. These parameters should not be prefixed.
        # Likewise, The creator of an LTI link can add custom key/value parameters
        # to a launch which are to be included with the launch of the LTI link.
        # In this case, we will automatically add `custom_` prefix before this parameters.
        # See http://www.imsglobal.org/LTI/v1p1p1/ltiIMGv1p1p1.html#_Toc316828520
        PARAMETERS = [
            "lti_message_type",
            "lti_version",
            "resource_link_id",
            "resource_link_title",
            "resource_link_description",
            "user_id",
            "user_image",
            "roles",
            "lis_person_name_given",
            "lis_person_name_family",
            "lis_person_name_full",
            "lis_person_contact_email_primary",
            "lis_person_sourcedid",
            "role_scope_mentor",
            "context_id",
            "context_type",
            "context_title",
            "context_label",
            "launch_presentation_locale",
            "launch_presentation_document_target",
            "launch_presentation_css_url",
            "launch_presentation_width",
            "launch_presentation_height",
            "launch_presentation_return_url",
            "tool_consumer_info_product_family_code",
            "tool_consumer_info_version",
            "tool_consumer_instance_guid",
            "tool_consumer_instance_name",
            "tool_consumer_instance_description",
            "tool_consumer_instance_url",
            "tool_consumer_instance_contact_email",
        ]

        client_key, client_secret = self.get_client_key_secret()

        # parsing custom parameters to dict
        custom_parameters = {}
        for custom_parameter in self.custom_parameters:
            try:
                param_name, param_value = [p.strip() for p in custom_parameter.split('=', 1)]
            except ValueError:
                raise LTIError('Could not parse custom parameter: {0!r}. \
                    Should be "x=y" string.'.format(custom_parameter))

            # LTI specs: 'custom_' should be prepended before each custom parameter, as pointed in link above.
            if param_name not in PARAMETERS:
                param_name = 'custom_' + param_name

            custom_parameters[unicode(param_name)] = unicode(param_value)

        return self.oauth_params(
            custom_parameters,
            client_key,
            client_secret,
        )

    def get_context(self):
        """
        Returns a context.
        """
        return {
            'input_fields': self.get_input_fields(),

            # These parameters do not participate in OAuth signing.
            'launch_url': self.launch_url.strip(),
            'element_id': self.location.html_id(),
            'element_class': self.category,
            'open_in_a_new_page': self.open_in_a_new_page,
            'display_name': self.display_name,
            'form_url': self.runtime.handler_url(self, 'preview_handler').rstrip('/?'),
        }

    def get_html(self):
        """
        Renders parameters to template.
        """
        return self.system.render_template('lti.html', self.get_context())

    @XBlock.handler
    def preview_handler(self, _, __):
        """
        This is called to get context with new oauth params to iframe.
        """
        template = self.system.render_template('lti_form.html', self.get_context())
        return Response(template, content_type='text/html')

    def get_user_id(self):
        user_id = self.runtime.anonymous_student_id
        assert user_id is not None
        return unicode(urllib.quote(user_id))

    def get_outcome_service_url(self):
        """
        Return URL for storing grades.

        To test LTI on sandbox we must use http scheme.

        While testing locally and on Jenkins, mock_lti_server use http.referer
        to obtain scheme, so it is ok to have http(s) anyway.
        """
        scheme = 'http' if 'sandbox' in self.system.hostname else 'https'
        uri = '{scheme}://{host}{path}'.format(
            scheme=scheme,
            host=self.system.hostname,
            path=self.runtime.handler_url(self, 'grade_handler', thirdparty=True).rstrip('/?')
        )
        return uri

    def get_resource_link_id(self):
        """
        This is an opaque unique identifier that the TC guarantees will be unique
        within the TC for every placement of the link.

        If the tool / activity is placed multiple times in the same context,
        each of those placements will be distinct.

        This value will also change if the item is exported from one system or
        context and imported into another system or context.

        This parameter is required.
        """
        return unicode(urllib.quote(self.id))

    def get_lis_result_sourcedid(self):
        """
        This field contains an identifier that indicates the LIS Result Identifier (if any)
        associated with this launch.  This field identifies a unique row and column within the
        TC gradebook.  This field is unique for every combination of context_id / resource_link_id / user_id.
        This value may change for a particular resource_link_id / user_id  from one launch to the next.
        The TP should only retain the most recent value for this field for a particular resource_link_id / user_id.
        This field is generally optional, but is required for grading.

        context_id is - is an opaque identifier that uniquely identifies the context that contains
        the link being launched.
        lti_id should be context_id by meaning.
        """
        return u':'.join(urllib.quote(i) for i in (self.lti_id, self.get_resource_link_id(), self.get_user_id()))


    def oauth_params(self, custom_parameters, client_key, client_secret):
        """
        Signs request and returns signature and OAuth parameters.

        `custom_paramters` is dict of parsed `custom_parameter` field
        `client_key` and `client_secret` are LTI tool credentials.

        Also *anonymous student id* is passed to template and therefore to LTI provider.
        """

        client = oauthlib.oauth1.Client(
            client_key=unicode(client_key),
            client_secret=unicode(client_secret)
        )

        # Must have parameters for correct signing from LTI:
        body = {
            u'user_id': self.get_user_id(),
            u'oauth_callback': u'about:blank',
            u'launch_presentation_return_url': '',
            u'lti_message_type': u'basic-lti-launch-request',
            u'lti_version': 'LTI-1p0',
            u'role': u'student',

            # Parameters required for grading:
            u'resource_link_id': self.get_resource_link_id(),
            u'lis_result_sourcedid': self.get_lis_result_sourcedid(),

        }

        if self.has_score:
            body.update({
                u'lis_outcome_service_url': self.get_outcome_service_url()
            })

        # Appending custom parameter for signing.
        body.update(custom_parameters)

        headers = {
            # This is needed for body encoding:
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        try:
            __, headers, __ = client.sign(
                unicode(self.launch_url.strip()),
                http_method=u'POST',
                body=body,
                headers=headers)
        except ValueError:  # Scheme not in url.
            # https://github.com/idan/oauthlib/blob/master/oauthlib/oauth1/rfc5849/signature.py#L136
            # Stubbing headers for now:
            headers = {
                u'Content-Type': u'application/x-www-form-urlencoded',
                u'Authorization': u'OAuth oauth_nonce="80966668944732164491378916897", \
oauth_timestamp="1378916897", oauth_version="1.0", oauth_signature_method="HMAC-SHA1", \
oauth_consumer_key="", oauth_signature="frVp4JuvT1mVXlxktiAUjQ7%2F1cw%3D"'}

        params = headers['Authorization']
        # Parse headers to pass to template as part of context:
        params = dict([param.strip().replace('"', '').split('=') for param in params.split(',')])

        params[u'oauth_nonce'] = params[u'OAuth oauth_nonce']
        del params[u'OAuth oauth_nonce']

        # oauthlib encodes signature with
        # 'Content-Type': 'application/x-www-form-urlencoded'
        # so '='' becomes '%3D'.
        # We send form via browser, so browser will encode it again,
        # So we need to decode signature back:
        params[u'oauth_signature'] = urllib.unquote(params[u'oauth_signature']).decode('utf8')

        # Add LTI parameters to OAuth parameters for sending in form.
        params.update(body)
        return params

    def max_score(self):
        return self.weight if self.has_score else None


    @XBlock.handler
    def grade_handler(self, request, dispatch):
        """
        This is called by courseware.module_render, to handle an AJAX call.

        Used only for grading. Returns XML response.

        Example of request body from LTI provider::

        <?xml version = "1.0" encoding = "UTF-8"?>
            <imsx_POXEnvelopeRequest xmlns = "some_link (may be not required)">
              <imsx_POXHeader>
                <imsx_POXRequestHeaderInfo>
                  <imsx_version>V1.0</imsx_version>
                  <imsx_messageIdentifier>528243ba5241b</imsx_messageIdentifier>
                </imsx_POXRequestHeaderInfo>
              </imsx_POXHeader>
              <imsx_POXBody>
                <replaceResultRequest>
                  <resultRecord>
                    <sourcedGUID>
                      <sourcedId>feb-123-456-2929::28883</sourcedId>
                    </sourcedGUID>
                    <result>
                      <resultScore>
                        <language>en-us</language>
                        <textString>0.4</textString>
                      </resultScore>
                    </result>
                  </resultRecord>
                </replaceResultRequest>
              </imsx_POXBody>
            </imsx_POXEnvelopeRequest>

        Example of correct/incorrect answer XML body:: see response_xml_template.
        """
        response_xml_template = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <imsx_POXEnvelopeResponse xmlns = "http://www.imsglobal.org/services/ltiv1p1/xsd/imsoms_v1p0">
                <imsx_POXHeader>
                    <imsx_POXResponseHeaderInfo>
                        <imsx_version>V1.0</imsx_version>
                        <imsx_messageIdentifier>{imsx_messageIdentifier}</imsx_messageIdentifier>
                        <imsx_statusInfo>
                            <imsx_codeMajor>{imsx_codeMajor}</imsx_codeMajor>
                            <imsx_severity>status</imsx_severity>
                            <imsx_description>{imsx_description}</imsx_description>
                            <imsx_messageRefIdentifier>
                            </imsx_messageRefIdentifier>
                        </imsx_statusInfo>
                    </imsx_POXResponseHeaderInfo>
                </imsx_POXHeader>
                <imsx_POXBody>{response}</imsx_POXBody>
            </imsx_POXEnvelopeResponse>
        """)
        # Returns when `action` is unsupported.
        # Supported actions:
        #   - replaceResultRequest.
        unsupported_values = {
            'imsx_codeMajor': 'unsupported',
            'imsx_description': 'Target does not support the requested operation.',
            'imsx_messageIdentifier': 'unknown',
            'response': ''
        }
        # Returns if:
        #   - score is out of range;
        #   - can't parse response from TP;
        #   - can't verify OAuth signing or OAuth signing is incorrect.
        failure_values = {
            'imsx_codeMajor': 'failure',
            'imsx_description': 'The request has failed.',
            'imsx_messageIdentifier': 'unknown',
            'response': ''
        }

        try:
            imsx_messageIdentifier, sourcedId, score, action = self.parse_grade_xml_body(request.body)
        except Exception as e:
            error_message = "Request body XML parsing error: " + escape(e.message)
            log.debug("[LTI]: " + error_message)
            failure_values['imsx_description'] = error_message
            return Response(response_xml_template.format(**failure_values), content_type="application/xml")

        # Verify OAuth signing.
        try:
            self.verify_oauth_body_sign(request)
        except (ValueError, LTIError) as e:
            failure_values['imsx_messageIdentifier'] = escape(imsx_messageIdentifier)
            error_message = "OAuth verification error: " + escape(e.message)
            failure_values['imsx_description'] = error_message
            log.debug("[LTI]: " + error_message)
            return Response(response_xml_template.format(**failure_values), content_type="application/xml")

        real_user = self.system.get_real_user(urllib.unquote(sourcedId.split(':')[-1]))
        if not real_user:  # that means we can't save to database, as we do not have real user id.
            failure_values['imsx_messageIdentifier'] = escape(imsx_messageIdentifier)
            failure_values['imsx_description'] = "User not found."
            return Response(response_xml_template.format(**failure_values), content_type="application/xml")

        if action == 'replaceResultRequest':
            self.system.publish(
                event={
                    'event_name': 'grade',
                    'value': score * self.max_score(),
                    'max_value': self.max_score(),
                },
                custom_user=real_user
            )

            values = {
                'imsx_codeMajor': 'success',
                'imsx_description': 'Score for {sourced_id} is now {score}'.format(sourced_id=sourcedId, score=score),
                'imsx_messageIdentifier': escape(imsx_messageIdentifier),
                'response': '<replaceResultResponse/>'
            }
            log.debug("[LTI]: Grade is saved.")
            return Response(response_xml_template.format(**values), content_type="application/xml")

        unsupported_values['imsx_messageIdentifier'] = escape(imsx_messageIdentifier)
        log.debug("[LTI]: Incorrect action.")
        return Response(response_xml_template.format(**unsupported_values), content_type='application/xml')


    @classmethod
    def parse_grade_xml_body(cls, body):
        """
        Parses XML from request.body and returns parsed data

        XML body should contain nsmap with namespace, that is specified in LTI specs.

        Returns tuple: imsx_messageIdentifier, sourcedId, score, action

        Raises Exception if can't parse.
        """
        lti_spec_namespace = "http://www.imsglobal.org/services/ltiv1p1/xsd/imsoms_v1p0"
        namespaces = {'def': lti_spec_namespace}

        data = body.strip().encode('utf-8')
        parser = etree.XMLParser(ns_clean=True, recover=True, encoding='utf-8')
        root = etree.fromstring(data, parser=parser)

        imsx_messageIdentifier = root.xpath("//def:imsx_messageIdentifier", namespaces=namespaces)[0].text
        sourcedId = root.xpath("//def:sourcedId", namespaces=namespaces)[0].text
        score = root.xpath("//def:textString", namespaces=namespaces)[0].text
        action = root.xpath("//def:imsx_POXBody", namespaces=namespaces)[0].getchildren()[0].tag.replace('{'+lti_spec_namespace+'}', '')
        # Raise exception if score is not float or not in range 0.0-1.0 regarding spec.
        score = float(score)
        if not 0 <= score <= 1:
            raise LTIError('score value outside the permitted range of 0-1.')

        return imsx_messageIdentifier, sourcedId, score, action

    def verify_oauth_body_sign(self, request):
        """
        Verify grade request from LTI provider using OAuth body signing.

        Uses http://oauth.googlecode.com/svn/spec/ext/body_hash/1.0/oauth-bodyhash.html::

            This specification extends the OAuth signature to include integrity checks on HTTP request bodies
            with content types other than application/x-www-form-urlencoded.

        Arguments:
            request: DjangoWebobRequest.

        Raises:
            LTIError if request is incorrect.
        """

        client_key, client_secret = self.get_client_key_secret()
        headers = {
            'Authorization':unicode(request.headers.get('Authorization')),
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        sha1 = hashlib.sha1()
        sha1.update(request.body)
        oauth_body_hash = base64.b64encode(sha1.digest())

        oauth_params = signature.collect_parameters(headers=headers, exclude_oauth_signature=False)
        oauth_headers =dict(oauth_params)
        oauth_signature = oauth_headers.pop('oauth_signature')

        mock_request = mock.Mock(
            uri=unicode(urllib.unquote(request.url)),
            http_method=unicode(request.method),
            params=oauth_headers.items(),
            signature=oauth_signature
        )
        if oauth_body_hash != oauth_headers.get('oauth_body_hash'):
            raise LTIError("OAuth body hash verification is failed.")
        if not signature.verify_hmac_sha1(mock_request, client_secret):
            raise LTIError("OAuth signature verification is failed.")

    def get_client_key_secret(self):
        """
        Obtains client_key and client_secret credentials from current course.
        """
        course_id = self.course_id
        course_location = CourseDescriptor.id_to_location(course_id)
        course = self.descriptor.runtime.modulestore.get_item(course_location)

        for lti_passport in course.lti_passports:
            try:
                lti_id, key, secret = [i.strip() for i in lti_passport.split(':')]
            except ValueError:
                raise LTIError('Could not parse LTI passport: {0!r}. \
                    Should be "id:key:secret" string.'.format(lti_passport))
            if lti_id == self.lti_id.strip():
                return key, secret
        return '', ''

class LTIDescriptor(LTIFields, MetadataOnlyEditingDescriptor, EmptyDataRawDescriptor):
    """
    Descriptor for LTI Xmodule.
    """
    module_class = LTIModule
    grade_handler = module_attr('grade_handler')
    preview_handler = module_attr('preview_handler')
