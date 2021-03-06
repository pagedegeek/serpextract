"""Utilities for extracting keyword information from search engine
referrers."""
import re
import logging
from itertools import groupby
from urlparse import urlparse, parse_qs, ParseResult

from iso3166 import countries
import pylru

# import pkg_resources
# with fallback for environments that lack it
try:
    import pkg_resources
except ImportError:
    import os

    class pkg_resources(object):
        """Fake pkg_resources interface which falls back to getting resources
        inside `serpextract`'s directory. (thank you tldextract!)
        """
        @classmethod
        def resource_stream(cls, package, resource_name):
            moddir = os.path.dirname(__file__)
            f = os.path.join(moddir, resource_name)
            return open(f)

# import cPickle
# for performance with a fallback on Python pickle
try:
    import cPickle as pickle
except ImportError:
    import pickle


__all__ = ('get_parser', 'is_serp', 'extract', 'get_all_query_params',
           'add_custom_parser', 'SearchEngineParser')

log = logging.getLogger('serpextract')

_country_codes = [country.alpha2.lower()
                  for country in countries]
# uk is not an official ISO-3166 country code, but it's used in top-level
# domains so we add it to our list see
# http://en.wikipedia.org/wiki/ISO_3166-1 for more information
_country_codes += ['uk']

# A LRUCache of domains to save us from having to do lots of regex matches
_domain_cache = pylru.lrucache(500)

# Naive search engine detection.  Look for \.?search\. in the netloc and then
# try to extract using common query params
_naive_re = re.compile(r'\.?search\.')
_naive_params = ('q', 'query', 'k', 'keyword', 'term',)

def _unicode_parse_qs(qs, **kwargs):
    """
    A wrapper around ``urlparse.parse_qs`` that converts unicode strings to
    UTF-8 to prevent ``urlparse.unquote`` from performing it's default decoding
    to latin-1 see http://hg.python.org/cpython/file/2.7/Lib/urlparse.py

    :param qs:       Percent-encoded query string to be parsed.
    :type qs:        ``basestring``

    :param kwargs:   Other keyword args passed onto ``parse_qs``.
    """
    if isinstance(qs, str):
        # Nothing to do
        return parse_qs(qs, **kwargs)

    qs = qs.encode('utf-8', 'ignore')
    query = parse_qs(qs, **kwargs)
    unicode_query = {}
    for key in query:
        uni_key = key.decode('utf-8', 'ignore')
        if uni_key == '':
            # because we ignore decode errors and only support utf-8 right now,
            # we could end up with a blank string which we ignore
            continue
        unicode_query[uni_key] = [p.decode('utf-8', 'ignore') for p in query[key]]
    return unicode_query


def _unicode_urlparse(url, encoding='utf-8', errors='ignore'):
    """
    Safely parse a URL into a :class:`urlparse.ParseResult` ensuring that
    all elements of the parse result are unicode.

    :param url:      A URL.
    :type url:       ``str``, ``unicode`` or :class:`urlparse.ParseResult`

    :param encoding: The string encoding assumed in the underlying ``str`` or
                     :class:`urlparse.ParseResult` (default is utf-8).
    :type encoding:  ``str``

    :param errors:   response from ``decode`` if string cannot be converted to
                     unicode given encoding (default is ignore).
    :type errors:    ``str``
    """
    if isinstance(url, str):
        url = url.decode(encoding, errors)
    elif isinstance(url, ParseResult):
        # Ensure every part is unicode because we can't rely on clients to do so
        parts = list(url)
        for i in range(len(parts)):
            if isinstance(parts[i], str):
                parts[i] = parts[i].decode(encoding, errors)
        return ParseResult(*parts)

    try:
        return urlparse(url)
    except ValueError:
        msg = u'Malformed URL "{}" could not parse'.format(url)
        log.debug(msg, exc_info=True)
        return None


def _serp_query_string(parse_result):
    """
    Some search engines contain the search keyword in the fragment so we
    build a version of a query string that contains the query string and
    the fragment.

    :param parse_result: A URL.
    :type parse_result:  :class:`urlparse.ParseResult`
    """
    query = parse_result.query
    if parse_result.fragment != '':
        query = u'{}&{}'.format(query, parse_result.fragment)

    return query


def _is_url_without_path_query_or_fragment(url_parts):
    """
    Determines if a URL has a blank path, query string and fragment.

    :param url_parts: A URL.
    :type url_parts:  :class:`urlparse.ParseResult`
    """
    return url_parts.path.strip('/') == '' and url_parts.query == '' \
           and url_parts.fragment == ''

_engines = None
def _get_search_engines():
    """
    Convert the OrderedDict of search engine parsers that we get from Piwik
    to a dictionary of SearchEngineParser objects.

    Cache this thing by storing in the global ``_engines``.
    """
    global _engines
    if _engines:
        return _engines

    piwik_engines = _get_piwik_engines()
    # Engine names are the first param of each of the search engine arrays
    # so we group by those guys, and create our new dictionary with that
    # order
    get_engine_name = lambda x: x[1][0]
    definitions_by_engine = groupby(piwik_engines.iteritems(), get_engine_name)
    _engines = {}

    for engine_name, rule_group in definitions_by_engine:
        defaults = {
            'extractor': None,
            'link_macro': None,
            'charsets': ['utf-8']
        }

        for i, rule in enumerate(rule_group):
            domain = rule[0]
            rule = rule[1][1:]
            if i == 0:
                defaults['extractor'] = rule[0]
                if len(rule) >= 2:
                    defaults['link_macro'] = rule[1]
                if len(rule) >= 3:
                    defaults['charsets'] = rule[2]

                _engines[domain] = SearchEngineParser(engine_name,
                                                      defaults['extractor'],
                                                      defaults['link_macro'],
                                                      defaults['charsets'])
                continue

            # Default args for SearchEngineParser
            args = [engine_name, defaults['extractor'],
                    defaults['link_macro'], defaults['charsets']]
            if len(rule) >= 1:
                args[1] = rule[0]

            if len(rule) >= 2:
                args[2] = rule[1]

            if len(rule) == 3:
                args[3] = rule[2]

            _engines[domain] = SearchEngineParser(*args)

    return _engines


def _get_piwik_engines():
    """
    Return the search engine parser definitions stored in this module. We don't
    cache this result since it's only supposed to be called once.
    """
    stream = pkg_resources.resource_stream
    with stream(__name__, 'search_engines.pickle') as picklestream:
        _piwik_engines = pickle.load(picklestream)

    return _piwik_engines


_get_lossy_domain_regex = None
def _get_lossy_domain(domain):
    """
    A lossy version of a domain/host to use as lookup in the ``_engines``
    dict.

    :param domain: A string that is the ``netloc`` portion of a URL.
    :type domain:  ``str``
    """
    global _domain_cache, _get_lossy_domain_regex

    if domain in _domain_cache:
        return _domain_cache[domain]

    if not _get_lossy_domain_regex:
        codes = '|'.join(_country_codes)
        _get_lossy_domain_regex = re.compile(
                r'^' # start of string
                r'(?:w+\d*\.|search\.|m\.)*' + # www. www1. search. m.
                r'((?P<ccsub>{})\.)?'.format(codes) + # country-code subdomain
                r'(?P<domain>.*?)' + # domain
                r'(?P<tld>\.(com|org|net|co|edu))?' + # tld
                r'(?P<tldcc>\.({}))?'.format(codes) + # country-code tld
                r'$') # all done

    res = _get_lossy_domain_regex.match(domain).groupdict()
    output = u'%s%s%s' % ('{}.' if res['ccsub'] else '',
                          res['domain'],
                          '.{}' if res['tldcc'] else res['tld'] or '')
    _domain_cache[domain] = output # Add to LRU cache
    return output


class ExtractResult(object):
    __slots__ = ('engine_name', 'keyword', 'parser')

    def __init__(self, engine_name, keyword, parser):
        self.engine_name = engine_name
        self.keyword = keyword
        self.parser = parser

    def __repr__(self):
        repr_fmt = 'ExtractResult(engine_name={!r}, keyword={!r}, parser={!r})'
        return repr_fmt.format(self.engine_name, self.keyword, self.parser)


class SearchEngineParser(object):
    """Handles persing logic for a single line in Piwik's list of search
    engines.

    Piwik's list for reference:

    https://raw.github.com/piwik/piwik/master/core/DataFiles/SearchEngines.php

    This class is not used directly since it already assumes you know the
    exact search engine you want to use to parse a URL. The main interface
    for users of this module is the :func:`extract` method.
    """
    __slots__ = ('engine_name', 'keyword_extractor', 'link_macro', 'charsets')

    def __init__(self, engine_name, keyword_extractor, link_macro, charsets):
        """New instance of a :class:`SearchEngineParser`.

        :param engine_name:         the friendly name of the engine (e.g.
                                    'Google')

        :param keyword_extractor:   a string or list of keyword extraction
                                    methods for this search engine.  If a
                                    single string, we assume we're extracting a
                                    query string param, if it's a string that
                                    starts with '/' then we extract from the
                                    path instead of query string

        :param link_macro:          a string indicating how to build a link to
                                    the search engine results page for a given
                                    keyword

        :param charsets:            a string or list of charsets to use to
                                    decode the keyword
        """
        self.engine_name = engine_name
        if isinstance(keyword_extractor, basestring):
            keyword_extractor = [keyword_extractor]
        self.keyword_extractor = keyword_extractor[:]
        for i, extractor in enumerate(self.keyword_extractor):
            # Pre-compile all the regular expressions
            if extractor.startswith('/'):
                extractor = extractor.strip('/')
                extractor = re.compile(extractor)
                self.keyword_extractor[i] = extractor

        self.link_macro = link_macro
        if isinstance(charsets, basestring):
            charsets = [charsets]
        self.charsets = [c.lower() for c in charsets]

    def get_serp_url(self, base_url, keyword):
        """
        Get a URL to a SERP for a given keyword.

        :param base_url: String of format ``'<scheme>://<netloc>'``.
        :type base_url:  ``str``

        :param keyword:  Search engine keyword.
        :type keyword:   ``str``

        :returns: a URL that links directly to a SERP for the given keyword.
        """
        if self.link_macro is None:
            return None

        link = u'{}/{}'.format(base_url, self.link_macro.format(k=keyword))
        return link

    def parse(self, url_parts):
        """
        Parse a SERP URL to extract the search keyword.

        :param serp_url: The SERP URL
        :type serp_url:  A :class:`urlparse.ParseResult` with all elements
                         as unicode

        :returns: An :class:`ExtractResult` instance.
        """
        original_query = _serp_query_string(url_parts)
        query = _unicode_parse_qs(original_query, keep_blank_values=True)

        keyword = None
        engine_name = self.engine_name

        if engine_name == 'Google Images' or \
           (engine_name == 'Google' and '/imgres' in original_query):
            # When using Google's image preview mode, it hides the keyword
            # within the prev query string param which itself contains a
            # path and query string
            # e.g. &prev=/search%3Fq%3Dimages%26sa%3DX%26biw%3D320%26bih%3D416%26tbm%3Disch
            engine_name = 'Google Images'
            if 'prev' in query:
                prev_query = _unicode_parse_qs(urlparse(query['prev'][0]).query)
                keyword = prev_query.get('q', [None])[0]
        elif engine_name == 'Google' and 'as_' in original_query:
            # Google has many different ways to filter results.  When some of
            # these filters are applied, we can no longer just look for the q
            # parameter so we look at additional query string arguments and
            # construct a keyword manually
            keys = []

            # Results should contain all of the words entered
            # Search Operator: None (same as normal search)
            key = query.get('as_q')
            if key:
              keys.append(key[0])
            # Results should contain any of these words
            # Search Operator: <keyword> [OR <keyword>]+
            key = query.get('as_oq')
            if key:
              key = key[0].replace('+', ' OR ')
              keys.append(key)
            # Results should match the exact phrase
            # Search Operator: "<keyword>"
            key = query.get('as_epq')
            if key:
              keys.append(u'"{}"'.format(key[0]))
            # Results should contain none of these words
            # Search Operator: -<keyword>
            key = query.get('as_eq')
            if key:
              keys.append(u'-{}'.format(key[0]))

            keyword = u' '.join(keys).strip()

        if engine_name == 'Google':
            # Check for usage of Google's top bar menu
            tbm = query.get('tbm', [None])[0]
            if tbm == 'isch':
                engine_name = 'Google Images'
            elif tbm == 'vid':
                engine_name = 'Google Video'
            elif tbm == 'shop':
                engine_name = 'Google Shopping'

        if keyword is not None:
            # Edge case found a keyword, exit quickly
            return ExtractResult(engine_name, keyword, self)

        # Otherwise we keep looking through the defined extractors
        for extractor in self.keyword_extractor:
            if not isinstance(extractor, basestring):
                # Regular expression extractor
                match = extractor.search(url_parts.path)
                if match:
                    keyword = match.group(1)
                    break
            else:
                # Search for keywords in query string
                if extractor in query:
                    # Take the last param in the qs because it should be the
                    # most recent
                    keyword = query[extractor][-1]

                # Now we have to check for a tricky case where it is a SERP
                # but just with no keyword as can be the case with Google
                # Images or DuckDuckGo
                if keyword is None and extractor == 'q' and \
                   engine_name in ('Google Images', 'DuckDuckGo'):
                    keyword = ''
                elif keyword is None and extractor == 'q' and \
                     engine_name == 'Google' and \
                     _is_url_without_path_query_or_fragment(url_parts):
                    keyword = ''

        if keyword is not None:
            return ExtractResult(engine_name, keyword, self)

    def __repr__(self):
        repr_fmt = ("SearchEngineParser(engine_name={!r}, "
                    "keyword_extractor={!r}, link_macro={!r}, charsets={!r})")
        return repr_fmt.format(
                        self.engine_name,
                        self.keyword_extractor,
                        self.link_macro,
                        self.charsets)


def add_custom_parser(match_rule, parser):
    """
    Add a custom search engine parser to the cached ``_engines`` list.

    :param match_rule: A match rule which is used by :func:`get_parser` to look
                       up a parser for a given domain/path.
    :type match_rule:  ``unicode``

    :param parser:     A custom parser.
    :type parser:      :class:`SearchEngineParser`
    """
    assert isinstance(match_rule, unicode)
    assert isinstance(parser, SearchEngineParser)

    global _engines
    _get_search_engines()  # Ensure that the default engine list is loaded

    _engines[match_rule] = parser


def get_all_query_params():
    """
    Return all the possible query string params for all search engines.

    :returns: a ``list`` of all the unique query string parameters that are
              used across the search engine definitions.
    """
    engines = _get_search_engines()
    all_params = set()
    _not_regex = lambda x: isinstance(x, basestring)
    for parser in engines.itervalues():
        # Find non-regex params
        params = set(filter(_not_regex, parser.keyword_extractor))
        all_params |= params

    return list(all_params)


def get_parser(referring_url):
    """
    Utility function to find a parser for a referring URL if it is a SERP.

    :param referring_url: Suspected SERP URL.
    :type referring_url:  ``str`` or :class:`urlparse.ParseResult`

    :returns: :class:`SearchEngineParser` object if one exists for URL,
              ``None`` otherwise.
    """
    engines = _get_search_engines()
    url_parts = _unicode_urlparse(referring_url)
    if url_parts is None:
        return None

    query = _serp_query_string(url_parts)

    domain = url_parts.netloc
    path = url_parts.path
    lossy_domain = _get_lossy_domain(url_parts.netloc)
    engine_key = url_parts.netloc

    # Try to find a parser in the engines list.  We go from most specific to
    # least specific order:
    # 1. <domain><path>
    # 2. <lossy_domain><path>
    # 3. <lossy_domain>
    # 4. <domain>
    # The final case has some special exceptions for things like Google custom
    # search engines, yahoo and yahoo images
    if u'{}{}'.format(domain, path) in engines:
        engine_key = u'{}{}'.format(domain, path)
    elif u'{}{}'.format(lossy_domain, path) in engines:
        engine_key = u'{}{}'.format(lossy_domain, path)
    elif lossy_domain in engines:
        engine_key = lossy_domain
    elif domain not in engines:
        if query[:14] == 'cx=partner-pub':
            # Google custom search engine
            engine_key = 'google.com/cse'
        elif url_parts.path[:28] == '/pemonitorhosted/ws/results/':
            # private-label search powered by InfoSpace Metasearch
            engine_key = 'wsdsold.infospace.com'
        elif '.images.search.yahoo.com' in url_parts.netloc:
            # Yahoo! Images
            engine_key = 'images.search.yahoo.com'
        elif '.search.yahoo.com' in url_parts.netloc:
            # Yahoo!
            engine_key = 'search.yahoo.com'
        else:
            return None

    return engines.get(engine_key)


def is_serp(referring_url, parser=None, use_naive_method=False):
    """
    Utility function to determine if a referring URL is a SERP.

    :param referring_url:    Suspected SERP URL.
    :type referring_url:     str or urlparse.ParseResult

    :param parser:           A search engine parser.
    :type parser:            :class:`SearchEngineParser` instance or
                             ``None``.

    :param use_naive_method: Whether or not to use a naive method of search
                             engine detection in the event that a parser does
                             not exist for the given ``referring_url``.  See
                             :func:`extract` for more information.
    :type use_naive_method:  ``True`` or ``False``

    :returns: ``True`` if SERP, ``False`` otherwise.
    """
    res = extract(referring_url, parser=parser,
                  use_naive_method=use_naive_method)
    return res is not None


def extract(serp_url, parser=None, lower_case=True, trimmed=True,
            collapse_whitespace=True, use_naive_method=False):
    """
    Parse a SERP URL and return information regarding the engine name,
    keyword and :class:`SearchEngineParser`.

    :param serp_url:            Suspected SERP URL to extract a keyword from.
    :type serp_url:             ``str`` or :class:`urlparse.ParseResult`

    :param parser:              Optionally pass in a parser if already
                                determined via call to get_parser.
    :type parser:               :class:`SearchEngineParser`

    :param lower_case:          Lower case the keyword.
    :type lower_case:           ``True`` or ``False``

    :param trimmed:             Trim keyword leading and trailing whitespace.
    :type trimmed:              ``True`` or ``False``

    :param collapse_whitespace: Collapse 2 or more ``\s`` characters into one
                                space ``' '``.
    :type collapse_whitespace:  ``True`` or ``False``

    :param use_naive_method:    In the event that a parser doesn't exist for
                                the given ``serp_url``, attempt to find an
                                instance of ``_naive_re_pattern`` in the netloc
                                of the ``serp_url``.  If found, try to extract
                                a keyword using ``_naive_params``.
    :type use_naive_method:     ``True`` or ``False``

    :returns: an :class:`ExtractResult` instance if ``serp_url`` is valid,
              ``None`` otherwise
    """
    # Software should only work with Unicode strings internally, converting
    # to a particular encoding on output.
    url_parts = _unicode_urlparse(serp_url)
    if url_parts is None:
        return None

    result = None
    if parser is None:
        parser = get_parser(url_parts)

    if parser is None:
        if not use_naive_method:
            return None  # Tried to get keyword from non SERP URL

        # Try to use naive method of detection
        if _naive_re.search(url_parts.netloc):
            query = _unicode_parse_qs(url_parts.query, keep_blank_values=True)
            for param in _naive_params:
                if param in query:
                    import tldextract
                    tld_res = tldextract.extract(url_parts.netloc)
                    return ExtractResult(tld_res.domain,
                                         query[param][0],
                                         None)

        return None  # Naive method could not detect a keyword either

    result = parser.parse(url_parts)

    if result is None:
        return None

    if lower_case:
        result.keyword = result.keyword.lower()
    if trimmed:
        result.keyword = result.keyword.strip()
    if collapse_whitespace:
        result.keyword = re.sub(r'\s+', ' ', result.keyword, re.UNICODE)

    return result


def main():
    import argparse
    import sys
    import re

    parser = argparse.ArgumentParser(
        description='Parse a SERP URL to extract engine name and keyword.')

    parser.add_argument('input', metavar='url', type=unicode, nargs='*',
                        help='A potential SERP URL')
    parser.add_argument('-l', '--list', default=False, action='store_true',
                        help='Print a list of all the SearchEngineParsers.')

    args = parser.parse_args()

    if args.list:
        engines = _get_search_engines()
        engines = sorted(engines.iteritems(), key=lambda x: x[1].engine_name)
        print '{:<30}{}'.format('Fuzzy Domain', 'Parser')
        for fuzzy_domain, parser in engines:
            print '{:<30}{}'.format(fuzzy_domain, parser)
        print '{} parsers.'.format(len(engines))
        sys.exit(0)

    if len(args.input) == 0:
        parser.print_usage()
        sys.exit(1)

    escape_quotes = lambda s: re.sub(r'"', '\\"', s)

    for url in args.input:
        res = extract(url)
        if res is None:
            res = ['""', '""']
        else:
            res = [escape_quotes(res.engine_name), escape_quotes(res.keyword)]
            res = [u'"{}"'.format(r) for r in res]
        print u','.join(res)

if __name__ == '__main__':
    main()
