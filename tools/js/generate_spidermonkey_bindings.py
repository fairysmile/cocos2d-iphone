#!/usr/bin/python
# ----------------------------------------------------------------------------
# Generates SpiderMonkey glue code after Objective-C code
#
# Author: Ricardo Quesada
# Copyright 2012 (C) Zynga, Inc
#
# Dual License: MIT or GPL v2.
# ----------------------------------------------------------------------------
'''
Generates SpiderMonkey glue code after Objective-C code
'''

__docformat__ = 'restructuredtext'


# python
import sys
import os
import re
import getopt
import glob
import ast
import xml.etree.ElementTree as ET
import itertools
import copy
import datetime
import ConfigParser
import string

class ParseException( Exception ):
    pass

#
# Globals
#
BINDINGS_PREFIX = 'js_bindings_'
PROXY_PREFIX = 'JSPROXY_'
METHOD_CONSTRUCTOR, METHOD_CLASS, METHOD_INIT, METHOD_REGULAR = xrange(4)


# xml2d recipe copied from here:
# http://code.activestate.com/recipes/577722-xml-to-python-dictionary-and-back/
def xml2d(e):
    """Convert an etree into a dict structure

    @type  e: etree.Element
    @param e: the root of the tree
    @return: The dictionary representation of the XML tree
    """
    def _xml2d(e):
        kids = dict(e.attrib)
        for k, g in itertools.groupby(e, lambda x: x.tag):
            g = [ _xml2d(x) for x in g ]
            kids[k]=  g
        return kids
    return { e.tag : _xml2d(e) }


class SpiderMonkey(object):

    @classmethod
    def parse_config_file( cls, config_file ):
        cp = ConfigParser.ConfigParser()
        cp.read(config_file)


        supported_options = {'obj_class_prefix_to_remove': '',
                             'classes_to_parse' : [],
                             'callback_methods' : [],
                             'classes_to_ignore' : [],
                             'bridge_support_file' : '',
                             'hierarchy_protocol_file' : '',
                             'inherit_class_methods' : True,
                             'functions_to_parse' : [],
                             'functions_to_ignore' : [],
                             }



        for s in cp.sections():
            config = copy.copy( supported_options )

            # Section is the config namespace
            config['namespace'] = s

            for o in cp.options(s):
                if not o in config:
                    print 'Ignoring unrecognized option: %s' % o
                    continue

                t = type( config[o] )
                if t == type(True):
                    v = cp.getboolean(s, o)
                elif t == type(1):
                    v = cp.getint(s, o )
                elif t == type(''):
                    v = cp.get(s, o )
                elif t == type([]):
                    v = cp.get(s, o )
                    v = v.replace('\t',' ')
                    v = v.replace('\n',' ')
                    v = v.split(' ')
                else:
                    raise Exception('Unsupported type' % str(t) )
                config[ o ] = v

            sp = SpiderMonkey( config )
            sp.parse()

    def __init__(self, config ):

        self.hierarchy_file = config['hierarchy_protocol_file']
        self.hierarchy = {}
        self.parse_hierarchy_file()

        self.bridgesupport_file = config['bridge_support_file']
        self.bs = {}
        self.parse_bridgesupport_file()

        self.namespace = config['namespace']

        #
        # Classes related
        #
        self.prefix = config['obj_class_prefix_to_remove']
        self.inherit_class_methods = config['inherit_class_methods']

        # Add here manually generated classes
        self.supported_classes = set(['NSObject'])
        self.init_classes_to_bind( config['classes_to_parse'] )
        self.init_classes_to_ignore( config['classes_to_ignore'] )

        self.callback_methods = {}
        callback_methods = config['callback_methods']
        for c in callback_methods:
            c,m = c.split('#')
            if not c in self.callback_methods:
                self.callback_methods[ c ] = []
            self.callback_methods[ c ].append( m )

        # In order to prevent parsing a class many times
        self.parsed_classes = []

        # Current method that is being parsed
        self.current_method = None

        #
        # function related
        #
        self.init_functions_to_bind( config['functions_to_parse'] )
        self.init_functions_to_ignore( config['functions_to_ignore'] )
        self.current_function = None
        self.callback_functions = []


    def parse_hierarchy_file( self ):
        f = open( self.hierarchy_file )
        self.hierarchy = ast.literal_eval( f.read() )
        f.close()

    def parse_bridgesupport_file( self ):
        p = ET.parse( self.bridgesupport_file )
        root = p.getroot()
        self.bs = xml2d( root )

    def init_functions_to_bind( self, functions ):
        self._functions_to_bind = set( functions )
        ref_list = []
        for k in self.bs['signatures']['function']:
            ref_list.append( k['name'] )
        self.functions_to_bind = self.expand_regexp_names( self._functions_to_bind, ref_list )

    def init_functions_to_ignore( self, klasses ):
        self._functions_to_ignore = klasses
        self.functions_to_ignore = self.expand_regexp_names( self._functions_to_ignore, self.functions_to_bind )

        print self.functions_to_ignore
        copy_set = copy.copy( self.functions_to_bind )
        for i in self.functions_to_bind:
            if i in self.functions_to_ignore:
                print 'Explicity removing %s from bindings...' % i
                copy_set.remove( i )

        self.functions_to_bind = copy_set

    def init_classes_to_bind( self, klasses ):
        self._classes_to_bind = set( klasses )
        ref_list = []
        for k in self.bs['signatures']['class']:
            ref_list.append( k['name'] )
        self.classes_to_bind = self.expand_regexp_names( self._classes_to_bind, ref_list )
        l = self.ancestors_of_classes_to_bind()
        s = set( self.classes_to_bind )
        self.classes_to_bind = s.union( set(l) )

    def init_classes_to_ignore( self, klasses ):
        self._classes_to_ignore = klasses
        self.classes_to_ignore = self.expand_regexp_names( self._classes_to_ignore, self.classes_to_bind )

        copy_set = copy.copy( self.classes_to_bind )
        for i in self.classes_to_bind:
            if i in self.classes_to_ignore:
                print 'Explicity removing %s from bindings...' % i
                copy_set.remove( i )

        self.classes_to_bind = copy_set
        self.supported_classes = self.supported_classes.union( copy_set )

    def ancestors_of_classes_to_bind ( self ):
        ancestors = []
        for klass in self.classes_to_bind:
            new_list = self.ancestors( klass, [klass] )
            ancestors.extend( new_list )
        return ancestors

    def ancestors( self, klass, list_of_ancestors ):
        if klass not in self.hierarchy:
            return list_of_ancestors

        info = self.hierarchy[ klass ]
        subclass =  info['subclass']
        if not subclass:
            return list_of_ancestors

        list_of_ancestors.append( subclass )

        return self.ancestors( subclass, list_of_ancestors )

    def expand_regexp_names( self, names_to_expand, list_of_names ):
        valid = []
        all_class_names = []
        for n in list_of_names:
            for regexp in names_to_expand:
                if not regexp or regexp=='':
                    continue
                # if last char is not a regexp modifier,
                # then append '$' to regexp
                last_char = regexp[-1]
                if last_char in string.letters or last_char in string.digits or last_char=='_':
                    result = re.match( regexp + '$', n )
                else:
                    result = re.match( regexp, n )
                if result:
                    valid.append( n )

        ret = list( set( valid ) )
        return ret

    #
    # Helpers
    #
    def get_parent_class( self, class_name ):
        try:
            parent = self.hierarchy[class_name]['subclass']
        except KeyError, e:
            return None
        return parent

    def get_class_method( self, class_name ):
        class_methods = []

        klass = None
        list_of_classes = self.bs['signatures']['class']
        for k in list_of_classes:
            if k['name'] == class_name:
                klass = k

        if not klass:
            raise Exception("Base class not found: %s" % class_name )

        for m in klass['method']:
            if self.is_class_method( m ):
                class_methods.append( m )
        return class_methods

    def get_struct_type_and_num_of_elements( self, struct ):
        # PRECOND: Structure must be valid

        # BridgeSupport to TypedArray
        bs_to_type_array =  { 'c' : 'TYPE_INT8',
                              'C' : 'TYPE_UINT8',
                              's' : 'TYPE_INT16',
                              'S' : 'TYPE_UINT16',
                              'i' : 'TYPE_INT32',
                              'I' : 'TYPE_UINT32',
                              'f' : 'TYPE_FLOAT32',
                              'd' : 'TYPE_FLOAT64',
                              }

        inner = struct.replace('{', '')
        inner = inner.replace('{', '')
        key,value = inner.split('=')

        k = value[0]
        if not k in bs_to_type_array:
            raise Exception('Structure cannot be converted')

        # returns type of structure and len
        return (bs_to_type_array[k], len(value) )

    def is_valid_structure( self, struct ):
        # Only support non-nested structures of only one type
        # valids:
        #   {xxx=CCC}
        #   {xxx=ff}
        # invalids:
        #   {xxx=CC{yyy=C}}
        #   {xxx=fC}

        if not struct:
            return False

        if struct[0] == '{' and struct[-1] == '}' and len( struct.split('{') ) == 2:
            inner = struct.replace('{', '')
            inner = inner.replace('{', '')
            key,value = inner.split('=')
            # values should be of the same type
            previous = None
            for c in value:
                if previous != None:
                    if previous != c:
                        return False
                    previous = c
            return True
        return False

    # whether or not the method is a constructor
    def is_class_constructor( self, method ):
        if self.is_class_method( method ) and 'retval' in method:
            retval = method['retval']
            dt = retval[0]['declared_type']

            # Should also check the naming convention. eg: 'spriteWith...'
            if dt == 'id':
                return True
        return False

    # whether or not the method is an initializer
    def is_method_initializer( self, method ):
        # Is this is a method ?
        if not 'selector' in method:
            return False

        if 'retval' in method:
            retval = method['retval']
            dt = retval[0]['declared_type']

            if method['selector'].startswith('init') and dt == 'id':
                return True
        return False

    def is_class_method( self, method ):
        return 'class_method' in method and method['class_method'] == 'true'

    def get_method_type( self, method ):
        if self.is_class_constructor( method ):
            method_type = METHOD_CONSTRUCTOR
        elif self.is_class_method( method ):
            method_type = METHOD_CLASS
        elif self.is_method_initializer(method):
            method_type = METHOD_INIT
        else:
            method_type = METHOD_REGULAR

        return method_type


    def convert_selector_name_to_native( self, name ):
        return name.replace(':','_')

    def convert_selector_name_to_js( self, selector ):
        name = ''
        parts = selector.split(':')
        for i,arg in enumerate(parts):
            if i==0:
                name += arg
            else:
                name += arg.capitalize()

        return name

    def generate_autogenerate_prefix( self, fd ):
        autogenerated_template = '''/*
* AUTOGENERATED FILE. DO NOT EDIT IT
* Generated by %s on %s
*/
'''
        fd.write( autogenerated_template % ( sys.argv[0], datetime.date.today() ) )

    #
    # "class" constructor and destructor
    #
    def generate_constructor( self, class_name ):

        # Global Variables
        # JSPROXY_CCNode
        # JSPROXY_CCNode
        constructor_globals = '''
JSClass* %s_class = NULL;
JSObject* %s_object = NULL;
'''

        # 1: JSPROXY_CCNode,
        # 2: JSPROXY_CCNode,
        # 8: possible callback code
        constructor_template = ''' // Constructor
JSBool %s_constructor(JSContext *cx, uint32_t argc, jsval *vp)
{
    JSObject *jsobj = [%s createJSObjectWithRealObject:nil context:cx];
    JS_SET_RVAL(cx, vp, OBJECT_TO_JSVAL(jsobj));

    %s

    return JS_TRUE;
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        self.mm_file.write( constructor_globals % ( proxy_class_name, proxy_class_name ) )
        self.mm_file.write( constructor_template % ( proxy_class_name, proxy_class_name, '/* no callbacks */' ) )

    def generate_destructor( self, class_name ):
        # 1: JSPROXY_CCNode,
        # 2: JSPROXY_CCNode, 3: JSPROXY_CCNode
        # JSPROXY_CCNode,
        # 4: possible callback code
        destructor_template = '''
// Destructor
void %s_finalize(JSContext *cx, JSObject *obj)
{
//	%%s *proxy = (%%s*)JS_GetPrivate(obj);
	%s *proxy = (%s*)get_proxy_for_jsobject(obj);

	if (proxy) {
		del_proxy_for_jsobject( obj );
		objc_setAssociatedObject([proxy realObj], &JSPROXY_association_proxy_key, nil, OBJC_ASSOCIATION_ASSIGN);
		%s
		[proxy release];
	}
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        self.mm_file.write( destructor_template % ( proxy_class_name,
                                                    proxy_class_name, proxy_class_name,
                                                    '/* no callbacks */' ) )

    #
    # Method generator functions
    #
    def generate_method_call_to_real_object( self, selector_name, num_of_args, ret_declared_type, args_declared_type, class_name, method_type ):

        args = selector_name.split(':')

        if method_type == METHOD_INIT:
            prefix = '\t%s *real = [(%s*)[proxy.klass alloc] ' % (class_name, class_name )
            suffix = '\n\t[proxy setRealObj: real];\n\t[real release];\n'
            suffix += '\n\tobjc_setAssociatedObject(real, &JSPROXY_association_proxy_key, proxy, OBJC_ASSOCIATION_ASSIGN);'
        elif method_type == METHOD_REGULAR:
            prefix = '\t%s *real = (%s*) [proxy realObj];\n\t' % (class_name, class_name)
            suffix = ''
            if ret_declared_type:
                prefix = prefix + 'ret_val = '
            prefix = prefix + '[real '
        elif method_type == METHOD_CONSTRUCTOR:
            prefix = '\t%s *ret_val = [%s ' % (class_name, class_name )
            suffix = ''
        elif method_type == METHOD_CLASS:
            if not ret_declared_type or ret_declared_type == 'void':
                prefix = '\t[%s ' % (class_name)
            else:
                prefix = '\t%s ret_val = [%s ' % (ret_declared_type, class_name )
            suffix = ''
        else:
            raise Exception('Invalid method type')

        # sanity check
        if num_of_args+1 != len(args):
            raise Exception('Error parsing...')

        call = ''

        for i,arg in enumerate(args):
            if num_of_args == 0:
                call += arg
            elif arg:   # empty arg?
                # cast needed to prevent compiler errors
                call += '%s:(%s)arg%d ' % (arg, args_declared_type[i], i)

        call += ' ];';

        return '%s%s%s' % (prefix, call, suffix )

    # special case: returning Object
    def generate_retval_object( self, declared_type, js_type ):
        object_template = '''
	JSObject *jsobj = get_or_create_jsobject_from_realobj( ret_val, cx );
	JS_SET_RVAL(cx, vp, OBJECT_TO_JSVAL(jsobj));
'''
        return object_template

    # special case: returning String
    def generate_retval_string( self, declared_type, js_type ):
        template = '''
	JSString *ret_obj = JS_NewStringCopyZ(cx, [ret_val UTF8String]);
	JS_SET_RVAL(cx, vp, STRING_TO_JSVAL(ret_obj) );
'''
        return template

    def generate_retval_array( self, declared_type, js_type ):
        template = '''
	jsval ret_jsval = NSArray_to_jsval( cx, (NSArray*) ret_val );
	JS_SET_RVAL(cx, vp, ret_jsval );
'''
        return template

    #
    # special case: returning CGPoint
    #
    def generate_retval_cgpoint( self, declared_type, js_type ):
        template = '''
	jsval ret_jsval = CGPoint_to_jsval( cx, ret_val );
	JS_SET_RVAL(cx, vp, ret_jsval);
'''
        return template

    # special case: returning CGSize
    def generate_retval_cgsize( self, declared_type, js_type ):
        template = '''
	jsval ret_jsval = CGSize_to_jsval( cx, ret_val );
	JS_SET_RVAL(cx, vp, ret_jsval);
'''
        return template

    def generate_retval_cgrect( self, declared_type, js_type ):
        template = '''
	jsval ret_jsval = CGRect_to_jsval( cx, ret_val );
	JS_SET_RVAL(cx, vp, ret_jsval);
'''
        return template

    def generate_retval_structure( self, declared_type, js_type ):
        template = '''
	JSObject *typedArray = js_CreateTypedArray(cx, js::TypedArray::%s, %d );
	%s* buffer = (%s*)JS_GetTypedArrayData(typedArray);
	*buffer = ret_val;
	JS_SET_RVAL(cx, vp, OBJECT_TO_JSVAL(typedArray));
	'''
        t, l = self.get_struct_type_and_num_of_elements( js_type )
        return template % (t, l,
                           declared_type, declared_type )

    def generate_retval( self, declared_type, js_type ):
        direct_convert = {
            'i' : 'INT_TO_JSVAL(ret_val)',
            'u' : 'INT_TO_JSVAL(ret_val)',
            'b' : 'BOOLEAN_TO_JSVAL(ret_val)',
            's' : 'STRING_TO_JSVAL(ret_val)',
            'd' : 'DOUBLE_TO_JSVAL(ret_val)',
            'c' : 'INT_TO_JSVAL(ret_val)',
            None : 'JSVAL_TRUE',
        }
        special_convert = {
            'o' : self.generate_retval_object,
            'S' : self.generate_retval_string,
            '[]': self.generate_retval_array,
        }

        special_declared_types = {
            'CGPoint' : self.generate_retval_cgpoint,
            'CGSize' :  self.generate_retval_cgsize,
            'CGRect' :  self.generate_retval_cgrect,
        }

        ret = ''
        if declared_type in special_declared_types:
            ret = special_declared_types[ declared_type ]( declared_type, js_type )
        elif self.is_valid_structure( js_type ):
            ret = self.generate_retval_structure( declared_type, js_type )
        elif js_type in special_convert:
            ret = special_convert[js_type]( declared_type, js_type )
        elif js_type in direct_convert:
            s = direct_convert[ js_type ]
            ret = '\tJS_SET_RVAL(cx, vp, %s);' % s
        else:
            raise Exception("Invalid key: %s" % js_type )

        return ret

    def validate_retval( self, method, class_name = None ):
        # Left column: BridgeSupport types
        # Right column: JS types
        supported_declared_types = {
            'CGPoint'   : 'N/A',
            'CGSize'    : 'N/A',
            'CGRect'    : 'N/A',
            'NSString*' : 'S',
            'NSArray*'  : '[]',
            'NSMutableArray*' : '[]',
            'CCArray*'  : '[]',
        }

        supported_types = {
            'f' : 'd',  # float
            'd' : 'd',  # double
            'i' : 'i',  # integer
            'I' : 'u',  # unsigned integer
            'c' : 'c',  # char
            'C' : 'c',  # unsigned char
            'B' : 'b',  # BOOL
            'v' :  None,  # void (for retval)
        }

#        s = method['selector']

        ret_js_type = None
        ret_declared_type = None

        # parse ret value
        if 'retval' in method:
            retval = method['retval']
            t = retval[0]['type']
            dt = retval[0]['declared_type']
            dt_class_name = dt.replace('*','')

            # Special case for initializer methods
            if self.is_method_initializer(method ):
                ret_js_type = None
                ret_declared_type = None

            # Special case for class constructors
            elif self.is_class_constructor( method ):
                ret_js_type = 'o'
                ret_declared_type = class_name

            # Part of supported declared types ?
            elif dt in supported_declared_types:
                ret_js_type = supported_declared_types[dt]
                ret_declared_type = dt

            # Part of supported types ?
            elif t in supported_types:
                if supported_types[t] == None:  # void type
                    ret_js_type = None
                    ret_declared_type = None
                else:
                    ret_js_type = supported_types[t]
                    ret_declared_type = retval[0]['declared_type']

            # special case for Objects
            elif t == '@' and dt_class_name in self.supported_classes:
                ret_js_type = 'o'
                ret_declared_type = dt
            elif self.is_valid_structure( t ):
                ret_js_type = t
                ret_declared_type =  dt
            else:
                raise ParseException('Unsupported return value %s' % dt)

        return (ret_js_type, ret_declared_type )

    def validate_arguments( self, method ):
        # Left column: BridgeSupport types
        # Right column: JS types
        supported_declared_types = {
            'CGPoint'   : 'N/A',
            'CGSize'    : 'N/A',
            'CGRect'    : 'N/A',
            'NSString*' : 'S',
            'NSArray*'  : '[]',
            'CCArray*'  : '[]',
            'NSMutableArray*' : '[]',
            'void (^)(id)' : 'f',
            'void (^)(CCNode *)' : 'f',
        }

        supported_types = {
            'f' : 'd',  # float
            'd' : 'd',  # double
            'i' : 'i',  # integer
            'I' : 'u',  # unsigned integer
            'c' : 'c',  # char
            'C' : 'c',  # unsigned char
            'B' : 'b',  # BOOL
            's' : 'c',  # short
        }

        args_js_type = []
        args_declared_type = []

        # parse arguments
        if 'arg' in method:
            args = method['arg']
            for arg in args:
                t = arg['type']
                dt = arg['declared_type']
                dt_class_name = dt.replace('*','')

                # IMPORTANT: 1st search on declared types.
                # NSString should be treated as a special case, not as a generic object
                if dt in supported_declared_types:
                    args_js_type.append( supported_declared_types[dt] )
                    args_declared_type.append( dt )
                elif self.is_valid_structure( t ):
                    args_js_type.append( t )
                    args_declared_type.append( dt )
                elif t in supported_types:
                    args_js_type.append( supported_types[t] )
                    args_declared_type.append( dt )
                # special case for Objects
                elif t == '@' and dt_class_name in self.supported_classes:
                    args_js_type.append( 'o' )
                    args_declared_type.append( dt )
                else:
                    raise ParseException("Unsupported argument: %s" % dt)

        return (args_js_type, args_declared_type)

    # Special case for string to NSString generator
    def generate_argument_string( self, i, arg_js_type, arg_declared_type ):
        template = '\tNSString *arg%d = jsval_to_nsstring( *argvp++, cx );\n'
        self.mm_file.write( template % i )

    # Special case for objects
    def generate_argument_object( self, i, arg_js_type, arg_declared_type ):
        object_template = '\t%s arg%d = (%s) jsval_to_nsobject( *argvp++, cx);\n'
        self.mm_file.write( object_template % (arg_declared_type, i, arg_declared_type ) )

    # CGPoint needs an special case since its internal structure changes
    # on the platform. On Mac it uses doubles and on iOS it uses floats
    # This function expect floats.
    def generate_argument_cgpoint( self, i, arg_js_type, arg_declared_type ):
        template = '\tCGPoint arg%d = jsval_to_CGPoint( *argvp++, cx );\n'
        self.mm_file.write( template % i )

    def generate_argument_cgsize( self, i, arg_js_type, arg_declared_type ):
        template = '\tCGSize arg%d = jsval_to_CGSize( *argvp++, cx );\n'
        self.mm_file.write( template % i )

    def generate_argument_cgrect( self, i, arg_js_type, arg_declared_type ):
        template = '\tCGRect arg%d = jsval_to_CGRect( *argvp++, cx );\n'
        self.mm_file.write( template % i )

    def generate_argument_struct( self, i, arg_js_type, arg_declared_type ):
        # This template assumes that the types will be the same on all platforms (eg: 64 and 32-bit platforms)
        template = '''
	JSObject *tmp_arg%d;
	JS_ValueToObject( cx, *argvp++, &tmp_arg%d );
	%s arg%d = *(%s*)JS_GetTypedArrayData( tmp_arg%d);
'''
        proxy_class_name = PROXY_PREFIX + arg_declared_type

        self.mm_file.write( template % (i,
                                        i,
                                        arg_declared_type, i, arg_declared_type, i ) )


    def generate_argument_array( self, i, arg_js_type, arg_declared_type ):
        template = '\tNSArray *arg%d = jsval_to_nsarray( *argvp++, cx );\n'
        self.mm_file.write( template % (i) )

    def generate_argument_function( self, i, arg_js_type, arg_declared_type ):
        template = '\tjs_block arg%d = jsval_to_block( *argvp++, cx, JS_THIS_OBJECT(cx, vp) );\n'
        self.mm_file.write( template % (i) )

    def generate_arguments( self, args_declared_type, args_js_type ):
        # b      JSBool          Boolean
        # c      uint16_t/jschar ECMA uint16_t, Unicode char
        # i      int32_t         ECMA int32_t
        # u      uint32_t        ECMA uint32_t
        # j      int32_t         Rounded int32_t (coordinate)
        # d      double          IEEE double
        # I      double          Integral IEEE double
        # S      JSString *      Unicode string, accessed by a JSString pointer
        # W      jschar *        Unicode character vector, 0-terminated (W for wide)
        # o      JSObject *      Object reference
        # f      JSFunction *    Function private
        # v      jsval           Argument value (no conversion)
        # *      N/A             Skip this argument (no vararg)
        # /      N/A             End of required arguments
        # More info:
        # https://developer.mozilla.org/en/SpiderMonkey/JSAPI_Reference/JS_ConvertArguments
        js_types_conversions = {
            'b' : ['JSBool',    'JS_ValueToBoolean'],
            'd' : ['double',    'JS_ValueToNumber'],
            'I' : ['double',    'JS_ValueToNumber'],    # double converted to string
            'i' : ['int32_t',   'JS_ValueToECMAInt32'],
            'j' : ['int32_t',   'JS_ValueToECMAInt32'],
            'u' : ['uint32_t',  'JS_ValueToECMAUint32'],
            'c' : ['uint16_t',  'JS_ValueToUint16'],
        }

        js_special_type_conversions =  {
            'S' : self.generate_argument_string,
            'o' : self.generate_argument_object,
            '[]': self.generate_argument_array,
            'f' : self.generate_argument_function,
        }

        js_declared_types_conversions = {
            'CGPoint' : self.generate_argument_cgpoint,
            'CGSize'  : self.generate_argument_cgsize,
            'CGRect'  : self.generate_argument_cgrect,
        }

        # First  time
        self.mm_file.write('\tjsval *argvp = JS_ARGV(cx,vp);\n')

        for i,arg in enumerate(args_js_type):

            if arg in js_types_conversions:
                t = js_types_conversions[arg]
                self.mm_file.write( '\t%s arg%d; %s( cx, *argvp++, &arg%d );\n' % ( t[0], i, t[1], i ) )
            elif arg in js_special_type_conversions:
                js_special_type_conversions[arg]( i, arg, args_declared_type[i] )
            elif args_declared_type[i] in js_declared_types_conversions:
                f = js_declared_types_conversions[ args_declared_type[i] ]
                f( i, arg, args_declared_type[i] )
            elif self.is_valid_structure( arg ):
                self.generate_argument_struct( i, arg, args_declared_type[i] )
            else:
                raise ParseException('Unsupported type: %s' % arg )

    def generate_method_prefix( self, class_name, converted_name, num_of_args, method_type ):
        # JSPROXY_CCNode, setPosition
        # "!" or ""
        # proxy.initialized = YES (or nothing)
        template_methodname = '''
JSBool %s_%s%s(JSContext *cx, uint32_t argc, jsval *vp) {
'''
        template_init = '''
	JSObject* obj = (JSObject *)JS_THIS_OBJECT(cx, vp);
//	JSPROXY_NSObject *proxy = (JSPROXY_NSObject*) JS_GetPrivate( obj );
	JSPROXY_NSObject *proxy = get_proxy_for_jsobject(obj);

	NSCAssert( proxy, @"Invalid Proxy object");
	NSCAssert( %s[proxy realObj], @"Object not initialzied. error");
'''

        # method name
        class_method = '_static' if self.is_class_method(self.current_method) else ''
        self.mm_file.write( template_methodname % ( PROXY_PREFIX+class_name, converted_name, class_method ) )

        # method asserts for instance methods
        if method_type == METHOD_INIT or method_type == METHOD_REGULAR:
            assert_init = '!' if method_type == METHOD_INIT else ''
            self.mm_file.write( template_init % assert_init )

        # Number of arguments
        method_assert_on_arguments = '\tNSCAssert( argc == %d, @"Invalid number of arguments" );\n'
        self.mm_file.write( method_assert_on_arguments % num_of_args )


    def generate_method_suffix( self ):
        end_template = '''
	return JS_TRUE;
}
'''
        self.mm_file.write( end_template )


    def generate_method( self, class_name, method ):

        # Variadic methods are not supported
        if 'variadic' in method and method['variadic'] == 'true':
            raise ParseException('variadic arguemnts not supported.')

        s = method['selector']

        # Don't generate methods that are defined as callbacks
        if class_name in self.callback_methods:
            if s in self.callback_methods[ class_name ]:
                raise ParseException('Method defined as callback. Ignoring.')

        args_js_type, args_declared_type = self.validate_arguments( method )
        ret_js_type, ret_declared_type = self.validate_retval( method, class_name )

        method_type = self.get_method_type( method )

        # writing...
        converted_name = self.convert_selector_name_to_native( s )

        num_of_args = len( args_declared_type )

        # writes method description
        self.mm_file.write( '\n// Arguments: %s\n// Ret value: %s' % ( ', '.join(args_declared_type), ret_declared_type ) )

        self.generate_method_prefix( class_name, converted_name, num_of_args, method_type )

        if len(args_js_type) > 0:
            self.generate_arguments( args_declared_type, args_js_type );

        if ret_declared_type and method_type==METHOD_REGULAR:
            self.mm_file.write( '\t%s ret_val;\n' % ret_declared_type )

        call_real = self.generate_method_call_to_real_object( s, num_of_args, ret_declared_type, args_declared_type, class_name, method_type )

        self.mm_file.write( '\n%s\n' % call_real )

        ret_string = self.generate_retval( ret_declared_type, ret_js_type )
        if not ret_string:
            raise ParseException('invalid return string')

        self.mm_file.write( ret_string )

        self.generate_method_suffix()

        return True

    def generate_methods( self, class_name, klass ):
        ok_methods = []
        ok_method_name = []

        # Parse methods defined in the Class
        self.is_a_protocol = False
        for m in klass['method']:
            self.current_method = m

            try:
                self.generate_method( class_name, m )
                ok_methods.append( m )
                ok_method_name.append( m['selector'] )
            except ParseException, e:
                sys.stderr.write( 'NOT OK: "%s#%s" Error: %s\n' % ( class_name, m['selector'], str(e) ) )

        self.current_method = None

        self.is_a_protocol = True

        # Parse methods defined in the Protocol
        if class_name in self.hierarchy:
            list_of_protocols = self.bs['signatures']['informal_protocol']
            protocols = self.hierarchy[ class_name ]['protocols']
            for protocol in protocols:
                for p in list_of_protocols:
                    # XXX Super slow
                    if p['name'] == protocol:

                        # Get the method object
                        for m in p['method']:
                            method_name = m['selector']

                            # avoid possible duplicates between Protocols and Classes
                            if not method_name in ok_method_name:
                                self.current_method = m
                                try:
                                    ok = self.generate_method( class_name, m )
                                    ok_methods.append( m )
                                    ok_method_name.append( m['selector'] )
                                except ParseException, e:
                                    sys.stderr.write( 'NOT OK: "%s#%s" Error: %s\n' % ( class_name, m['selector'], str(e) ) )

        # Parse class methods from base classes
        if self.inherit_class_methods:
            parent = self.get_parent_class( class_name )
            while (parent != None) and (not parent in self.classes_to_ignore):
                class_methods = self.get_class_method( parent )
                for cm in class_methods:
                    if not cm['selector'] in ok_method_name:
                        self.current_method = cm
                        try:
                            ok = self.generate_method( class_name, cm )
                            ok_methods.append( cm )
                            ok_method_name.append( cm['selector'] )
                        except ParseException, e:
                            sys.stderr.write( 'NOT OK: "%s#%s" Error: %s\n' % ( class_name, cm['selector'], str(e) ) )
                parent = self.get_parent_class( parent )

        self.current_method = None
        self.is_a_protocol = False

        return ok_methods

    def generate_class_mm_prefix( self ):
        import_template = '''
// needed for callbacks from objective-c to JS
#import <objc/runtime.h>
#import "JRSwizzle.h"

#import "jstypedarray.h"
#import "ScriptingCore.h"

#import "%s%s_classes.h"

'''
        self.generate_autogenerate_prefix( self.mm_file )
        self.mm_file.write( import_template % (BINDINGS_PREFIX, self.namespace) )

    def generate_pragma_mark( self, class_name, fd ):
        pragm_mark = '''
/*
 * %s
 */
#pragma mark - %s
'''
        fd.write( pragm_mark % (class_name, class_name) )

    def generate_class_header_prefix( self ):
        self.generate_autogenerate_prefix( self.h_file )
        self.h_file.write('#import "%sNSObject.h"\n' % BINDINGS_PREFIX )

    def generate_class_header( self, class_name, parent_name ):
        # JSPROXXY_CCNode
        # JSPROXXY_CCNode
        # JSPROXY_CCNode, JSPROXY_NSObject
        header_template = '''

#ifdef __cplusplus
extern "C" {
#endif

void %s_createClass(JSContext *cx, JSObject* globalObj, const char* name );

extern JSObject *%s_object;
extern JSClass *%s_class;

#ifdef __cplusplus
}
#endif


/* Proxy class */
@interface %s : %s
{
}
'''
        header_template_end = '''
@end
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )

        self.generate_pragma_mark( class_name, self.h_file )

        self.h_file.write( header_template % (  proxy_class_name,
                                                proxy_class_name,
                                                proxy_class_name,
                                                proxy_class_name, PROXY_PREFIX + parent_name  ) )
        # callback code should be added here
        self.h_file.write( header_template_end )

    def generate_implementation_callback( self, class_name ):
        # onEnter
        # onEnter
        # onEnter
        template = '''
-(void) %s
{
	if (_jsObj) {
		JSContext* cx = [[ScriptingCore sharedInstance] globalContext];
		JSBool found;
		JS_HasProperty(cx, _jsObj, "%s", &found);
		if (found == JS_TRUE) {
			jsval rval, fval;
			JS_GetProperty(cx, _jsObj, "%s", &fval);
			JS_CallFunctionValue(cx, _jsObj, fval, 0, 0, &rval);
		}
	}
}
'''
        if class_name in self.callback_methods:
            for m in self.callback_methods[ class_name ]:
                self.mm_file.write( template % ( m,
                                                 m,
                                                 m ) )

    def generate_implementation_swizzle( self, class_name ):
        # CCNode
        # CCNode
        template_prefix = '''
+(void) swizzleMethods
{
	[super swizzleMethods];

	static BOOL %s_already_swizzled = NO;
	if( ! %s_already_swizzled ) {
		NSError *error;
'''
        # CCNode, onEnter, onEnter
        template_middle = '''
		if( ! [%s jr_swizzleMethod:@selector(%s) withMethod:@selector(%s_JSHook) error:&error] )
			NSLog(@"Error swizzling %%@", error);
'''
        # CCNode
        template_suffix = '''
		%s_already_swizzled = YES;
	}
}
'''

        if class_name in self.callback_methods:
            self.mm_file.write(  template_prefix % ( class_name, class_name ) )
            for m in self.callback_methods[ class_name ]:
                self.mm_file.write( template_middle % ( class_name, m, m ) )

            self.mm_file.write(  template_suffix % ( class_name ) )

    def generate_implementation( self, class_name ):

        create_object_template_prefix = '''
+(JSObject*) createJSObjectWithRealObject:(id)realObj context:(JSContext*)cx
{
	JSObject *jsobj = JS_NewObject(cx, %s_class, %s_object, NULL);
	%s *proxy = [[%s alloc] initWithJSObject:jsobj class:[%s class]];
	[proxy setRealObj:realObj];
//	JS_SetPrivate(jsobj, proxy);
	set_proxy_for_jsobject(proxy, jsobj);

	if( realObj )
		objc_setAssociatedObject(realObj, &JSPROXY_association_proxy_key, proxy, OBJC_ASSOCIATION_ASSIGN);

	[self swizzleMethods];
'''

        create_object_template_suffix = '''
	return jsobj;
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )

        self.mm_file.write( '\n@implementation %s\n' % proxy_class_name )

        self.mm_file.write( create_object_template_prefix % (proxy_class_name, proxy_class_name,
                                                             proxy_class_name, proxy_class_name,
                                                             class_name
                                                             ) )

        self.mm_file.write( create_object_template_suffix )

        self.generate_implementation_swizzle( class_name )

        self.generate_implementation_callback( class_name )

        self.mm_file.write( '\n@end\n' )

    def generate_createClass_function( self, class_name, parent_name, ok_methods ):
        # 1-12: JSPROXY_CCNode
        implementation_template = '''
void %s_createClass(JSContext *cx, JSObject* globalObj, const char* name )
{
	%s_class = (JSClass *)calloc(1, sizeof(JSClass));
	%s_class->name = name;
	%s_class->addProperty = JS_PropertyStub;
	%s_class->delProperty = JS_PropertyStub;
	%s_class->getProperty = JS_PropertyStub;
	%s_class->setProperty = JS_StrictPropertyStub;
	%s_class->enumerate = JS_EnumerateStub;
	%s_class->resolve = JS_ResolveStub;
	%s_class->convert = JS_ConvertStub;
	%s_class->finalize = %s_finalize;
	%s_class->flags = JSCLASS_HAS_PRIVATE;
'''

        # Properties
        properties_template = '''
	static JSPropertySpec properties[] = {
		{0, 0, 0, 0, 0}
	};
'''
        functions_template_start = '\tstatic JSFunctionSpec funcs[] = {\n'
        functions_template_end = '\t\tJS_FS_END\n\t};\n'

        static_functions_template_start = '\tstatic JSFunctionSpec st_funcs[] = {\n'
        static_functions_template_end = '\t\tJS_FS_END\n\t};\n'

        # 1: JSPROXY_CCNode
        # 2: JSPROXY_NSObject
        # 3-4: JSPROXY_CCNode
        init_class_template = '''
	%s_object = JS_InitClass(cx, globalObj, %s_object, %s_class, %s_constructor,0,properties,funcs,NULL,st_funcs);
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        proxy_parent_name = '%s%s' % (PROXY_PREFIX, parent_name )

        self.mm_file.write( implementation_template % ( proxy_class_name,
                                                        proxy_class_name, proxy_class_name, proxy_class_name,
                                                        proxy_class_name, proxy_class_name, proxy_class_name,
                                                        proxy_class_name, proxy_class_name, proxy_class_name,
                                                        proxy_class_name, proxy_class_name, proxy_class_name ) )

        self.mm_file.write( properties_template )

        js_fn = '\t\tJS_FN("%s", %s, 1, JSPROP_PERMANENT | JSPROP_SHARED %s),\n'


        instance_method_buffer = ''
        class_method_buffer = ''
        for method in ok_methods:

            class_method = '_static' if self.is_class_method(method) else ''

            js_name = self.convert_selector_name_to_js( method['selector'] )
            cb_name = self.convert_selector_name_to_native( method['selector'] )

            if self.is_class_constructor( method ):
                entry = js_fn % (js_name, proxy_class_name + '_' + cb_name + class_method, '| JSPROP_ENUMERATE' ) # | JSFUN_CONSTRUCTOR
            else:
                entry = js_fn % (js_name, proxy_class_name + '_' + cb_name + class_method, '| JSPROP_ENUMERATE' )

            if self.is_class_method( method ):
                class_method_buffer += entry
            else:
                instance_method_buffer += entry

        # instance methods entry point
        self.mm_file.write( functions_template_start )
        self.mm_file.write( instance_method_buffer )
        self.mm_file.write( functions_template_end )

        # class methods entry point
        self.mm_file.write( static_functions_template_start )
        self.mm_file.write( class_method_buffer )
        self.mm_file.write( static_functions_template_end )

        self.mm_file.write( init_class_template % ( proxy_class_name, proxy_parent_name, proxy_class_name, proxy_class_name ) )

    def generate_callback_code( self, class_name ):
        # CCNode
        template_prefix = '@implementation %s (SpiderMonkey)\n'

        # onEnter
        # PROXYJS_CCNode
        template = '''
-(void) %s_JSHook
{
	%s *proxy = objc_getAssociatedObject(self, &JSPROXY_association_proxy_key);
	if( proxy )
		[proxy %s];

	[self %s_JSHook];
}
'''
        template_suffix = '@end\n'

        proxy_class_name = PROXY_PREFIX + class_name

        if class_name in self.callback_methods:

            self.mm_file.write( template_prefix % class_name )
            for m in self.callback_methods[ class_name ]:
                self.mm_file.write( template % ( m,
                                                 proxy_class_name,
                                                 m,
                                                 m) )

            self.mm_file.write( template_suffix )

    def generate_class_mm( self, klass, class_name, parent_name ):

        self.generate_pragma_mark( class_name, self.mm_file )
        self.generate_constructor( class_name )
        self.generate_destructor( class_name )

        ok_methods = self.generate_methods( class_name, klass )

        self.generate_createClass_function( class_name, parent_name, ok_methods )
        self.generate_implementation( class_name )

        self.generate_callback_code( class_name )

    def generate_class_binding( self, class_name ):

        # Ignore NSObject. Already registerd
        if not class_name or class_name in self.classes_to_ignore or class_name in self.parsed_classes:
            return

        parent = self.hierarchy[class_name]['subclass']
        self.generate_class_binding( parent )

        self.parsed_classes.append( class_name )

        signatures = self.bs['signatures']
        classes = signatures['class']
        klass = None

        parent_name = self.hierarchy[ class_name ]['subclass']

        # XXX: Super slow. Add them into a dictionary
        for c in classes:
            if c['name'] == class_name:
                klass = c
                break

        if not klass:
            raise Exception("Class not found: '%s'. Check file: '%s'" % (class_name, self.bridgesupport_file ) )

        methods = klass['method']

        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )

        self.generate_class_mm( klass, class_name, parent_name )
        self.generate_class_header( class_name, parent_name )

    def generate_class_registration( self, klass ):
        # only supported classes
        if not klass or klass in self.classes_to_ignore:
            return

        if not klass in self.classes_registered:
            parent = self.hierarchy[klass]['subclass']
            self.generate_class_registration( parent )

            klass_wo_prefix = klass
            if klass.startswith( self.prefix ):
                klass_wo_prefix = klass[len( self.prefix) : ]

            self.class_registration_file.write('%s%s_createClass(_cx, %s, "%s");\n' % ( PROXY_PREFIX, klass, self.namespace, klass_wo_prefix ) )
            self.classes_registered.append( klass )

    def generate_classes_registration( self ):

        self.classes_registered = []

        self.class_registration_file = open( '%s%s_classes_registration.h' % (BINDINGS_PREFIX, self.namespace), 'w' )
        self.generate_autogenerate_prefix( self.class_registration_file )

        for klass in self.supported_classes:
            self.generate_class_registration( klass )

        self.class_registration_file.close()

    def generate_function_mm_prefix( self ):
        import_template = '''
#import "jstypedarray.h"
#import "ScriptingCore.h"
#import "js_obj_conversions.h"
#import "%s%s_functions.h"
'''
        self.generate_autogenerate_prefix( self.mm_file )
        self.mm_file.write( import_template % (BINDINGS_PREFIX, self.namespace) )

    def generate_function_header_prefix( self ):
        self.generate_autogenerate_prefix( self.h_file )
        self.h_file.write('''
#ifdef __cplusplus
extern "C" {
#endif
''')

    def generate_function_header_suffix( self ):
        self.h_file.write('''
#ifdef __cplusplus
}
#endif
''')

    def generate_function_declaration( self, func_name ):
        # JSPROXY_ccDrawPoint
        template_funcname = 'JSBool %s%s(JSContext *cx, uint32_t argc, jsval *vp);\n'
        self.h_file.write( template_funcname % ( PROXY_PREFIX, func_name ) )

    def generate_function_call_to_real_object( self, func_name, num_of_args, ret_declared_type, args_declared_type ):

        if ret_declared_type:
            prefix = '\tret_val = %s(' % func_name
        else:
            prefix = '\t%s(' % func_name

        call = ''

        for i,dt in enumerate(args_declared_type):
            # cast needed to prevent compiler errors
            if i >0:
                call += ', '
            call += '(%s)arg%d ' % (dt, i)

        call += ' );';

        return '%s%s' % (prefix, call )

    def generate_function_prefix( self, func_name, num_of_args ):
        # JSPROXY_ccDrawPoint
        template_funcname = '''
JSBool %s%s(JSContext *cx, uint32_t argc, jsval *vp) {
'''
        self.mm_file.write( template_funcname % ( PROXY_PREFIX, func_name ) )

        # Number of arguments
        self.mm_file.write( '\tNSCAssert( argc == %d, @"Invalid number of arguments" );\n' % num_of_args )

    def generate_function_suffix( self ):
        end_template = '''
	return JS_TRUE;
}
'''
        self.mm_file.write( end_template )

    def generate_function_binding( self, function ):

        func_name = function['name']

        # Don't generate functions that are defined as callbacks
        if func_name in self.callback_functions:
            raise ParseException('Function defined as callback. Ignoring %s' % func_name)

        args_js_type, args_declared_type = self.validate_arguments( function )
        ret_js_type, ret_declared_type = self.validate_retval( function )

        num_of_args = len( args_declared_type )

        # writes method description
        self.mm_file.write( '\n// Arguments: %s\n// Ret value: %s' % ( ', '.join(args_declared_type), ret_declared_type ) )

        self.generate_function_prefix( func_name, num_of_args )

        if len(args_js_type) > 0:
            self.generate_arguments( args_declared_type, args_js_type );

        if ret_declared_type:
            self.mm_file.write( '\t%s ret_val;\n' % ret_declared_type )

        call_real = self.generate_function_call_to_real_object( func_name, num_of_args, ret_declared_type, args_declared_type )
        self.mm_file.write( '\n%s\n' % call_real )

        ret_string = self.generate_retval( ret_declared_type, ret_js_type )
        if not ret_string:
            raise ParseException('invalid return string')

        self.mm_file.write( ret_string )

        self.generate_function_suffix()

        return True

    def generate_function_registration( self, function ):
        pass

    def generate_functions_registration( self ):
        for func in self.functions_to_bind:
            self.generate_function_registration( func )

    def generate_bindings( self ):

        #
        # Classes
        #
        self.h_file = open( '%s%s_classes.h' % ( BINDINGS_PREFIX, self.namespace), 'w' )
        self.generate_class_header_prefix()
        self.mm_file = open( '%s%s_classes.mm' % (BINDINGS_PREFIX, self.namespace), 'w' )
        self.generate_class_mm_prefix()

        for klass in self.classes_to_bind:
            self.generate_class_binding( klass )

        self.h_file.close()
        self.mm_file.close()

        self.generate_classes_registration()

        #
        # Free Functions
        #
        self.h_file = open( '%s%s_functions.h' % ( BINDINGS_PREFIX, self.namespace), 'w' )
        self.generate_function_header_prefix()
        self.mm_file = open( '%s%s_functions.mm' % (BINDINGS_PREFIX, self.namespace), 'w' )
        self.generate_function_mm_prefix()

        for f in self.bs['signatures']['function']:
            if f['name'] in self.functions_to_bind:
                try:
                    self.generate_function_binding( f )
                    self.generate_function_declaration( f['name'] )
                except ParseException, e:
                    sys.stderr.write( 'NOT OK: "%s" Error: %s\n' % (  f['name'], str(e) ) )

        self.generate_function_header_suffix()
        self.h_file.close()
        self.mm_file.close()

        self.generate_functions_registration()


    def parse( self ):
        self.generate_bindings()

def help():
    print "%s v0.1 - Script that generates glue code between Objective-C and Javascript (Spidermonkey)" % sys.argv[0]
    print "Usage:"
    print "\t-c --config-file\tConfiguration file needed to generate the glue code."
    print "\nExample:"
    print "\t%s -c cocos2d-config.ini" % sys.argv[0]
    sys.exit(-1)

if __name__ == "__main__":
    if len( sys.argv ) == 1:
        help()

    configfile = None

    argv = sys.argv[1:]
    try:
        opts, args = getopt.getopt(argv, "c:", ["config-file="])

        for opt, arg in opts:
            if opt in ("-c", "--config-file"):
                configfile = arg
    except getopt.GetoptError,e:
        print e
        opts, args = getopt.getopt(argv, "", [])

    if args == None:
        help()

    SpiderMonkey.parse_config_file( configfile )