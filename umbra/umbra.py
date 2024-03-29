#!/usr/bin/env python
from json import dumps, loads
import os,sys,argparse, urllib.request, urllib.error, urllib.parse
import websocket
import time
import uuid
import logging
import threading
from kombu import Connection, Exchange, Queue
logging.basicConfig(level=logging.INFO)

class Umbra:
	def __init__(self, port, amqpurl):
		self.cmd_id = 0
		self.chrome_debug_port = port
		self.producer = None
		self.current_socket = False
		self.amqpurl = amqpurl
		self.launch_tab_socket = self.get_websocket(self.on_open)
		self.producer_lock = threading.Lock()
		threading.Thread(target=self.launch_tab_socket.run_forever).start()

	def get_websocket(self, on_open, url=None):
		def fetch_debugging_json():
			return loads(urllib.request.urlopen("http://localhost:%s/json" % self.chrome_debug_port).read().decode('utf-8').replace("\\n",""))
			time.sleep(0.5)
		debug_info = []
		while not (debug_info and 'webSocketDebuggerUrl' in debug_info[0]):
			debug_info = fetch_debugging_json()
			if url:
				debug_info = [x for x in debug_info if x['url'] == url]
		return_socket = websocket.WebSocketApp(debug_info[0]['webSocketDebuggerUrl'], on_message = self.handle_message)
		return_socket.on_open = on_open
		return return_socket

	def handle_message(self, ws, message):
		message = loads(message)
		if "method" in message.keys() and message["method"] == "Network.requestWillBeSent":
			request_queue = Queue('requests',  routing_key='request', exchange=self.umbra_exchange)
			with self.producer_lock:
				self.producer.publish(message['params']['request'],routing_key='request', exchange=self.umbra_exchange, declare=[request_queue])


	def start_amqp(self):
		self.umbra_exchange = Exchange('umbra', 'direct', durable=True)
		url_queue = Queue('urls',  routing_key='url', exchange=self.umbra_exchange)
		with Connection(self.amqpurl) as conn:
			self.producer = conn.Producer(serializer='json')
			with conn.Consumer(url_queue, callbacks=[self.fetch_url]) as consumer:
				while True:
					conn.drain_events()

	def on_open(self, ws):
		threading.Thread(target=self.start_amqp).start()


	def send_command(self,tab=None, **kwargs):
		if not tab:
			tab = self.launch_tab_socket
		command = {}
		command.update(kwargs)
		self.cmd_id += 1
		command['id'] = self.cmd_id
		tab.send(dumps(command))

	def fetch_url(self, body, message):
		while self.current_socket:
			time.sleep(1)
		self.current_socket = True
		url = body['url']
		new_page = 'data:text/html;charset=utf-8,<html><body>%s</body></html>' % str(uuid.uuid4())
		self.send_command(method="Runtime.evaluate", params={"expression":"window.open('%s');" % new_page})
		def on_open(ws):
			ws.on_message=self.handle_message
			self.send_command(tab=ws, method="Network.enable")
			self.send_command(tab=ws, method="Runtime.evaluate", params={"expression":"document.location = '%s';" % url})
			def do_close():
				time.sleep(10)
				self.send_command(tab=ws, method="Runtime.evaluate", params={"expression":"window.open('', '_self', ''); window.close(); "})
				self.current_socket = False
			threading.Thread(target=do_close).start()
		socket = self.get_websocket(on_open, new_page)
		message.ack()
		threading.Thread(target=socket.run_forever).start()

class Chrome():
	def __init__(self, port, executable, browser_wait):
		self.port = port
		self.executable = executable
		self.browser_wait=browser_wait

	def __enter__(self):
		import psutil, subprocess
		self.chrome_process = subprocess.Popen([self.executable, "--disable-web-sockets", "--incognito", "--temp-profile", "--remote-debugging-port=%s" % self.port])
		start = time.time()
		import socket
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		while sock.connect_ex(('127.0.0.1',int(self.port))) != 0 and (time.time() - start) < float(self.browser_wait):
			time.sleep(0.1)


	def __exit__(self, *args):
		self.chrome_process.kill()

def main():
	arg_parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]),
			description='umbra - Browser automation tool',
			formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	arg_parser.add_argument('-w', '--browser-wait', dest='browser_wait', default='10',
			help='Seconds to wait for browser initialization')
	arg_parser.add_argument('-e', '--executable', dest='executable', default='chromium-browser',
			help='Executable to use to invoke chrome')
	arg_parser.add_argument('-p', '--port', dest='port', default='9222',
			help='Port to have invoked chrome listen on for debugging connections')
	arg_parser.add_argument('-u', '--url', dest='amqpurl', default='amqp://guest:guest@localhost:5672//',
			help='URL identifying the amqp server to talk to')
	args = arg_parser.parse_args(args=sys.argv[1:])
	with Chrome(args.port, args.executable, args.browser_wait):
		Umbra(args.port, args.amqpurl)
		while True:
			time.sleep(1)

if __name__ == "__main__":
	main()
