#!/usr/bin/env python

import port80
import port443
import requests
import re
import datetime
from time import strptime
import base64
import wget
import json
import csv
import os
import utils

from sslyze.server_connectivity import ServerConnectivityInfo
from sslyze.plugins.certificate_info_plugin import CertificateInfoPlugin
from sslyze.plugins.hsts_plugin import HstsPlugin

DEFAULT_USER_AGENT = "pshtt, https scanning"
USER_AGENT = os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)

# Defaults to 1 second
TIMEOUT = 1

# The fields we're collecting, will be keys in JSON and
# column headers in CSV.
HEADERS = [
    "Domain", "Live", "Redirect",
    "Valid HTTPS", "Defaults HTTPS", "Downgrades HTTPS",
    "Strictly Forces HTTPS", "HTTPS Bad Chain", "HTTPS Bad Host Name",
    "Expired Cert", "Weak Signature Chain", "HSTS", "HSTS Header",
    "HSTS Max Age", "HSTS All Subdomains", "HSTS Preload",
    "HSTS Preload Ready", "HSTS Preloaded",
    "Broken Root", "Broken WWW"
]

preload_list = None

def inspect(domain):
    http = port80.port80("http://%s" % domain, domain)
    httpwww = port80.port80("http://www.%s" % domain, domain)
    https = port443.port443("https://%s" % domain, domain)
    httpswww = port443.port443("https://www.%s" % domain, domain)

    basic_check(http)
    basic_check(httpwww)
    basic_check(https)
    basic_check(httpswww)

    https_check(https)
    https_check(httpswww)

    return {
        'Domain': domain,
        'Live': is_live(http, httpwww, https, httpswww),
        'Redirect': is_redirect(http, httpwww, https, httpswww),
        'Valid HTTPS': is_valid_https(http, httpwww, https, httpswww),
        'Defaults HTTPS': is_defaults_to_https(http, httpwww, https, httpswww),
        'Downgrades HTTPS': is_downgrades_https(http, httpwww, https, httpswww),
        'Strictly Forces HTTPS': is_strictly_forces_https(http, httpwww, https, httpswww),
        'HTTPS Bad Chain': is_bad_chain(http, httpwww, https, httpswww),
        'HTTPS Bad Host Name': is_bad_hostname(http, httpwww, https, httpswww),
        'Expired Cert': is_expired_cert(http, httpwww, https, httpswww),
        'Weak Signature Chain': is_weak_signature(http, httpwww, https, httpswww),
        'HSTS': is_hsts(http, httpwww, https, httpswww),
        'HSTS Header': hsts_header(http, httpwww, https, httpswww),
        'HSTS Max Age': hsts_max_age(http, httpwww, https, httpswww),
        'HSTS All Subdomains': is_hsts_all_subdomains(http, httpwww, https, httpswww),
        'HSTS Preload': is_hsts_preload(http, httpwww, https, httpswww),
        'HSTS Preload Ready': is_hsts_preload_ready(http, httpwww, https, httpswww),
        'HSTS Preloaded': is_hsts_preloaded(http, httpwww, https, httpswww),
        'Broken Root': is_broken_root(http, httpwww, https, httpswww),
        'Broken WWW': is_broken_www(http, httpwww, https, httpswww)
    }


def basic_check(endpoint):
    print("pinging %s..." % endpoint.endpoint)
    # First check if the endpoint is live
    try:
        r = requests.get(
            endpoint.endpoint,
            data={'User-Agent': USER_AGENT},
            timeout=TIMEOUT
        )
        # If status code starts with a 3, it is a redirect
        if len(r.history) > 0 and str(r.history[0].status_code).startswith('3'):
            endpoint.redirect = True
            endpoint.redirect_to = r.url
        endpoint.live = True
    # The endpoint is live but there is a bad cert
    except requests.exceptions.SSLError:
        # TODO: this is too broad, won't always be chain
        endpoint.https_bad_chain = True
        endpoint.live = True
        # If there is a bad cert and the domain is not an https endpoint it is a redirect
        if endpoint.endpoint[5:] == "http:":
            endpoint.redirect = True
    # Endpoint is not live
    except:
        pass


def https_check(endpoint):
    print("sslyzing %s..." % endpoint.endpoint)

    # Use sslyze to check for HSTS
    try:
        # remove the https:// from prefix for sslyze
        hostname = endpoint.endpoint[8:]
        server_info = ServerConnectivityInfo(hostname=hostname, port=443)
        server_info.test_connectivity_to_server()

        # Call Plugin directly
        plugin = HstsPlugin()
        # Run HSTS plugin from sslyze returning HSTS header
        plugin_result = plugin.process_task(server_info, 'hsts')

        # Sslyze will return OK if HSTS exists
        if "OK" in plugin_result.as_text()[1]:
            endpoint.hsts = "True"
            # Send HSTS header for parsing
            hsts_header_handler(endpoint, plugin_result.as_text()[1])

        # Call plugin directly
        cert_plugin = CertificateInfoPlugin()
        cert_plugin_result = cert_plugin.process_task(server_info, 'certinfo_basic')
        # Parsing Sslzye output for results by line
        for i in cert_plugin_result.as_text():
            # Check for cert expiration
            if "Not After" in i:
                expired_cert(i, endpoint)
            # Check for Hostname validation
            elif "Hostname Validation" in i:
                bad_hostname(i, endpoint)
            # Check if Cert is trusted based on CA Stores
            elif "CA Store" in i:
                bad_chain(i, endpoint)
            # Check for s SHA1 Cert in the Cert Chain
            elif "Weak Signature" in i:
                weak_signature(i, endpoint)
                break
    except:
        # No valid hsts
        pass


def hsts_header_handler(endpoint, header):
    # Remove colons, semi colons, and commas from header
    var = re.sub('[;,:]', ' ', header)
    # Removes extra spaces from header
    x = ' '.join(var.split())
    # Split sslyze text from header
    endpoint.hsts_header = x.partition("received ")[-1]
    temp = endpoint.hsts_header.split()
    # Set max age to the string after max-age
    endpoint.hsts_max_age = temp[0][len("max-age="):]
    # check if hsts includes sub domains
    if 'includesubdomains' in endpoint.hsts_header.lower():
        endpoint.hsts_all_subdomains = True
    # Check is hsts is preload
    if 'preload' in endpoint.hsts_header.lower():
        endpoint.hsts_preload = True


def bad_chain(trusted, endpoint):
    # If the cert is not trusted by mozilla it is a bad chain
    if "FAILED" in trusted:
        endpoint.https_bad_chain = True


def bad_hostname(hostname_validation, endpoint):
    # If hostname validation fails
    if "FAILED" in hostname_validation:
        endpoint.https_bad_hostname = True


def expired_cert(expired_date, endpoint):
    # Split the time into an list of subtrings
    temp = expired_date.split()
    # Convert the date returned by sslyze to be comparable to current time
    if datetime.datetime(int(temp[5]), strptime(temp[2], '%b').tm_mon, int(temp[3])) < datetime.datetime.now():
        endpoint.https_expired_cert = True


def weak_signature(weak_sig, endpoint):
    # If a SHA-1 cert exists in the cert chain
    if "INSECURE" in weak_sig:
        endpoint.weak_signature = True


##
# Judgment calls based on observed endpoint data.

def is_live(http, httpwww, https, httpswww):
    # Domain is live if a single endpoint is live
    if http.live or httpwww.live or https.live or httpswww.live:
        return "True"
    else:
        return "False"


def is_redirect(http, httpwww, https, httpswww):
    # Domain is a redirect if any of the endpoints redirect
    if http.redirect or httpwww.redirect or https.redirect or httpswww.redirect:
        return "True"
    else:
        return "False"


def is_valid_https(http, httpwww, https, httpswww):
    # Domain has valid https if either https enpoints are live or a http redirects to https
    if https.live or httpswww.live:
        return "True"
    elif http.redirect_to[:5] == "https" or httpwww.redirect_to[:5] == "https":
        return "True"
    else:
        return "False"


def is_defaults_to_https(http, httpwww, https, httpswww):
    # Domain defaults to https if http endpoint forwards to https
    if http.redirect or httpwww.redirect:
        if http.redirect_to[:5] == "https" or httpwww.redirect_to[:5] == "https":
            return "True"
        else:
            return "False"
    else:
        return "False"


def is_downgrades_https(http, httpwww, https, httpswww):
    # Domain downgrades if https endpoint redirects to http
    if https.redirect or httpswww.redirect:
        if https.redirect_to[:5] == "http:" or httpswww.redirect_to[:5] == "http:":
            return "True"
        else:
            return "False"
    else:
        return "False"


def is_strictly_forces_https(http, httpwww, https, httpswww):
    # Domain Strictly forces https if https is live and http is not,
    # if both http forward to https endpoints or if one http forwards to https and the other is not live
    if ((not http.live and not httpwww.live) and (https.live or httpswww.live)):
        return "True"
    elif http.redirect_to[:5] == "https" and httpwww.redirect_to[:5] == "https":
        return "True"
    elif http.redirect_to[:5] == "https" and not httpwww.live:
        return "True"
    elif httpwww.redirect_to[:5] == "https" and not http.live:
        return "True"
    else:
        return "False"


def is_bad_chain(http, httpwww, https, httpswww):
    # Domain has a bad chain if either https endpoints contain a bad chain
    if https.https_bad_chain or httpswww.https_bad_chain:
        return "True"
    else:
        return "False"


def is_bad_hostname(http, httpwww, https, httpswww):
    # Domain has a bad hostname if either https endpoint fails hostname validation
    if https.https_bad_hostname or httpswww.https_bad_hostname:
        return "True"
    else:
        return "False"


def is_hsts(http, httpwww, https, httpswww):
    # Domain has hsts ONLY if the https and not the www subdomain has strict transport in the header
    if https.hsts:
        return "True"
    else:
        return "False"

def hsts_header(http, httpwww, https, httpswww):
    # Returns the https HSTS header
    if https.hsts:
        return https.hsts_header
    else:
        return ""


def hsts_max_age(http, httpwww, https, httpswww):
    # Returns the https HSTS max age
    if https.hsts:
        return https.hsts_max_age
    else:
        return ""


def is_hsts_all_subdomains(http, httpwww, https, httpswww):
    # Returns if the https endpoint has "includesubdomains"
    if https.hsts_all_subdomains:
        return "True"
    else:
        return "False"


def is_hsts_preload_ready(http, httpwww, https, httpswww):
    # returns if the hsts header exists, has a max age, includes subdomains, and includes preload
    if https.hsts and https.hsts_max_age != "" and https.hsts_all_subdomains and https.hsts_preload:
        return "True"
    else:
        return "False"


def is_hsts_preload(http, httpwww, https, httpswww):
    # Returns if https endpoint has preload in hsts header
    if https.hsts_preload:
        return "True"
    else:
        return "False"


def is_broken_root(http, httpwww, https, httpswww):
    # Returns if both root domains are unreachable
    if not http.live and not https.live:
        return "True"
    else:
        return "False"


def is_broken_www(http, httpwww, https, httpswww):
    # Returns if both www sub domains are unreachable
    if not httpwww.live and not httpswww.live:
        return "True"
    else:
        return "False"


def is_expired_cert(http, httpwww, https, httpswww):
    # Returns if the either https endpoint has an expired cert
    if https.https_expired_cert or httpswww.https_expired_cert:
        return "True"
    else:
        return "False"


def is_weak_signature(http, httpwww, https, httpswww):
    # Returns true if either https endpoint contains a SHA1 cert in the chain
    if https.weak_signature or httpswww.weak_signature:
        return "True"
    else:
        return "False"


# Preloaded will only be checked if the domain is preload ready for performance
def is_hsts_preloaded(http, httpwww, https, httpswww):
    # Returns if a domain is on the chromium preload list
    if https.hsts_preload and https.base_domain in preload_list:
        return "True"
    else:
        return "False"


def create_preload_list():
    print("Downloading preload list...")

    preload_cache = "./preload-list.json"
    preload_json = None

    if os.path.exists(preload_cache):
        print("Using cached Chrome preload list. Delete %s to clear the cache." % preload_cache)
        preload_json = json.loads(open(preload_cache).read())
    else:
        print("Fetching Chrome preload list from source...")

        # Downloads the chromium preloaded domain list and sets it to a global set
        file_url = 'https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json?format=TEXT'
        wget.download(file_url, out=preload_cache, bar=None)

        raw = open(preload_cache, 'r').read()

        # To avoid parsing the contents of the file out of the source tree viewer's
        # HTML, we download it as a raw file. googlesource.com Base64-encodes the
        # file to avoid potential content injection issues, so we need to decode it
        # before using it. https://code.google.com/p/gitiles/issues/detail?id=7
        raw = base64.b64decode(raw).decode('utf-8')

        # The .json file contains '//' comments, which are not actually valid JSON,
        # and confuse Python's JSON decoder. Begone, foul comments!
        raw = ''.join([re.sub(r'^\s*//.*$', '', line)
                       for line in raw.splitlines()])

        preload_json = json.loads(raw)
        utils.write(utils.json_for(preload_json), preload_cache)

    return {entry['name'] for entry in preload_json['entries']}


# Output a CSV string for an array of results, with a
# header row, and with header fields in the desired order.
def csv_for(results, out_filename):
    out_file = open(out_filename, 'w')
    writer = csv.writer(out_file)

    writer.writerow(HEADERS)

    for result in results:
        row = []
        for header in HEADERS:
            row.append(result[header])
        writer.writerow(row)

    out_file.close()

def inspect_domains(domains):
    # Download HSTS preload list, caches locally.
    global preload_list
    preload_list = create_preload_list()

    # For every given domain, get inspect data.
    results = []
    for domain in domains:
        results.append(inspect(domain))

    return results
