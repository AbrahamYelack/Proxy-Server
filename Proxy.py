import socket
import sys
import os
import argparse
import re
import email.utils as eut
import datetime
from pytz import timezone
import threading

BUFFER_SIZE = 1000000

# Helper function to get current time based on the timeZone given
def getCurrentTime(timeZone):
    return datetime.datetime.now(timeZone).replace(tzinfo=None)

# Helper function to parse date and have the same format as the date returned from getCurrentTime()
def parseDate(date):
    return datetime.datetime(*eut.parsedate(date)[:6])

# Helper function for cleanup and exit
def cleanup_and_exit(message, socket_to_close=None):
    """
    Display an error message, perform cleanup, and exit the program.
    """
    print(message)
    if socket_to_close:
        socket_to_close.close()
    sys.exit()

# Function to handle cache directives in the header field
def handle_cache_directives(cache_data, cache_path, client_socket):
    """
    Handle cache directives in the header field.
    If the cache is suitable for reuse, send back contents of the cached file.
    """
    cache_directive = [i for i in cache_data if 'Cache-Control' in i]
    max_age = None

    if cache_directive:
        cache_directive = cache_directive[0]
    else:
        cache_directive = None

    if cache_directive:
        index = cache_directive.index(":") + 2
        directives = cache_directive[index:len(cache_directive):1]
        directives = directives.split(", ")
        max_age = [i for i in directives if 'max-age=' in i]
        if not max_age:
            max_age = None

    if max_age is not None:
        max_age = max_age[0]
        max_age = int(max_age.split("=")[1])
        date = [i for i in cache_data if 'Date' in i][0]
        index = date.index(":") + 2
        date = date[index:len(date):1]
        date = parseDate(date)
        current_time = getCurrentTime(timezone('GMT'))
        age = current_time - date
        age = age.seconds

        if age > max_age:
            os.remove(cache_path)
            cache_file = None
            cache_data = None

    if cache_data:
        for line in cache_data:
            client_socket.send(line)
    else:
        raise IOError("Cache File Not Suitable or Not Found!")

    cache_file.close()
    print('Cache file closed')

# Function to handle client request
def handle_client_request(client_socket, args):
    """
    Handle the incoming client request.
    Check if the requested resource is in the cache, and either serve from cache or fetch from the origin server.
    """
    try:
        client_request = client_socket.recv(BUFFER_SIZE)
        print('Received request:')
        print('<', client_request)

        request_parts = client_request.split()
        method, URI, version = request_parts

        print('Method:\t\t', method)
        print('URI:\t\t', URI)
        print('Version:\t', version)
        print('\n\n')

        URI = re.sub('^(/?)http(s?)://', '', URI, 1)
        URI = URI.replace('/..', '')

        resource_parts = URI.split('/', 1)
        hostname, resource = resource_parts[0], '/'

        if len(resource_parts) == 2:
            resource += resource_parts[1]

        print('Requested Resource:\t', resource)

        cache_path = './' + hostname + resource
        if cache_path.endswith('/'):
            cache_path += 'default'

        print('Cache location:\t\t', cache_path)

        if os.path.isfile(cache_path):
            cache_file = open(cache_path, "r")
            cache_data = cache_file.readlines()

            print('Cache hit! Loading from cache file:', cache_path)

            handle_cache_directives(cache_data, cache_path, client_socket)

        else:
            handle_cache_miss(client_socket, hostname, resource, cache_path)

    except Exception as e:
        cleanup_and_exit(f'Error handling client request: {str(e)}', client_socket)

# Function to handle cache miss
def handle_cache_miss(client_socket, hostname, resource, cache_path):
    """
    Handle the case when the requested resource is not in the cache.
    Fetch the resource from the origin server, serve it to the client, and cache the response if applicable.
    """
    try:
        # Establish a connection to the origin server
        origin_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print('Connecting to:\t\t', hostname, '\n')

        host_address = socket.gethostbyname(hostname)
        origin_server_socket.connect((str(host_address), 80))

        # Construct the request to be sent to the origin server
        origin_server_request_line = "GET " + resource + " HTTP/1.1"
        origin_server_request_header = "Host: " + hostname
        origin_server_request = origin_server_request_line + '\r\n' + origin_server_request_header + '\r\n\r\n'

        print('Forwarding request to origin server:')
        for line in origin_server_request.split('\r\n'):
            print('> ' + line)

        # Send the request to the origin server
        try:
            origin_server_socket.sendall(origin_server_request)
        except socket.error:
            print('Send failed')
            sys.exit()

        print('Request sent to origin server\n')
        origin_server_socket.write(origin_server_request)

        # Use to store the response from the origin server
        data = ''

        # Get the response from the origin server
        data = origin_server_socket.recv(BUFFER_SIZE)

        # Use to determine if this response should be cached
        is_cache = True

        # Get the response code from the response
        data_lines = data.split('\r\n')
        response_code = data_lines[0]

        # Decide which content should be cached based on response code
        response_code = response_code.split(" ")[1]
        cacheable_codes = {"200", "203", "206", "300", "301", "410"}

        if response_code not in cacheable_codes:
            # Check if the response has a 'max-age' directive, if not, do not cache
            max_age = [i for i in data_lines if 'max-age=' in i]
            if not max_age:
                is_cache = False

        # Send the data to the client
        client_socket.send(data)

        # Cache the content if it should be cached
        if is_cache:
            # Create a new file in the cache for the requested resource
            # Also send the response in the buffer to the client socket
            # and save the corresponding file in the cache
            cache_dir, file = os.path.split(cache_path)
            print('Cached directory ' + cache_dir)
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
            cache_file = open(cache_path, 'wb')

            # Save the origin server response (data) in the cache file
            cache_file.write(data)
            cache_file.close()

            print('Cache file closed')

        # Finished sending to the origin server - shutdown socket writes
        origin_server_socket.shutdown(socket.SHUT_WR)

        print('Origin server done sending')
        origin_server_socket.close()

        # Shutdown client socket for writing
        client_socket.shutdown(socket.SHUT_WR)
        print('Client socket shutdown for writing')

    except Exception as e:
        cleanup_and_exit(f'Error handling cache miss: {str(e)}', client_socket)

# Function to handle individual client requests in a thread
def handle_client(client_socket, args):
    """
    Function to handle individual client requests in a thread.
    """
    try:
        handle_client_request(client_socket, args)
    except Exception as e:
        cleanup_and_exit(f'Error handling client: {str(e)}', client_socket)

# Main function to run the proxy server
def main():
    """
    Main function to run the proxy server.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('hostname', help='the IP Address Of Proxy Server')
    parser.add_argument('port', help='the port number of the proxy server')
    args = parser.parse_args()

    server_socket = None
    try:
        # Create a server socket
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print('Connected socket')
    except:
        cleanup_and_exit('Failed to create socket')

    try:
        # Bind the server socket to the specified address and port
        server_socket.bind((args.hostname, int(args.port)))
        print('Port is bound')
    except:
        cleanup_and_exit('Port is in use', server_socket)

    try:
        # Listen for incoming connections
        server_socket.listen(5)
        print('Listening to socket')
    except:
        cleanup_and_exit('Failed to listen', server_socket)

    while True:
        print('\n\nWaiting connection...')

        # Accept incoming connection
        client_socket, addr = server_socket.accept()
        print('Accepted connection from ', addr)

        # Create a new thread to handle the client request
        client_thread = threading.Thread(target=handle_client, args=(client_socket, args))
        client_thread.start()

if __name__ == "__main__":
    main()
