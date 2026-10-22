import json, os, threading, tempfile, unittest
from unittest.mock import patch
from PIL import Image
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from src.studio.harness import propose
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  self.server.captured = {'path':self.path,'data':data}
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  answer=json.dumps({'summary':'测试响应','shots':[]},ensure_ascii=False)
  if 'messages' in self.path:
   events=[('message_start',{'type':'message_start','message':{'id':'msg_test','type':'message','role':'assistant','model':'claude-sonnet-4-5','content':[],'stop_reason':None,'usage':{'input_tokens':10,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':answer}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':20}}),('message_stop',{'type':'message_stop'})]
   for event,obj in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(obj)+'\n\n').encode())
  elif 'responses' in self.path:
   response={'id':'resp_test','object':'response','created_at':1,'status':'in_progress','model':'gpt-4o','output':[]}
   item={'id':'msg_test','type':'message','role':'assistant','status':'in_progress','content':[]}
   events=[{'type':'response.created','response':response},{'type':'response.output_item.added','output_index':0,'item':item},{'type':'response.content_part.added','output_index':0,'item_id':'msg_test','content_index':0,'part':{'type':'output_text','text':'','annotations':[]}},{'type':'response.output_text.delta','output_index':0,'item_id':'msg_test','content_index':0,'delta':answer},{'type':'response.output_text.done','output_index':0,'item_id':'msg_test','content_index':0,'text':answer}]
   item=dict(item,status='completed',content=[{'type':'output_text','text':answer,'annotations':[]}])
   events += [{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':dict(response,status='completed',output=[item],usage={'input_tokens':10,'output_tokens':20,'total_tokens':30})}]
   for obj in events:self.wfile.write(('event: '+obj['type']+'\ndata: '+json.dumps(obj)+'\n\n').encode())
  else:
   for delta,finish in [({'role':'assistant','content':answer},None),({},'stop')]:
    obj={'id':'chatcmpl-test','object':'chat.completion.chunk','created':1,'model':'gpt-4o','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
    self.wfile.write(('data: '+json.dumps(obj)+'\n\n').encode())
   self.wfile.write(b'data: [DONE]\n\n')

@unittest.skipUnless(os.environ.get('STUDIO_HARNESS_TESTS') == '1', 'explicit SDK integration opt-in')
class HarnessProtocolTests(unittest.TestCase):
 def test_gpt_and_claude_images_without_tools(self):
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
  threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   for provider,model,key in [('openai','gpt-4o','OPENAI_API_KEY'),('anthropic','claude-sonnet-4-5','ANTHROPIC_API_KEY')]:
    with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{key:'test-only'}):
     image=Path(temp)/'test.jpg';Image.new('RGB',(40,40),'green').save(image)
     base=f'http://127.0.0.1:{server.server_port}' + ('/v1' if provider=='openai' else '')
     result=propose(Path(temp)/'home',provider,model,'Return JSON',images=[('test.jpg',image)],base_url=base)
     self.assertEqual(result['shots'],[])
     self.assertEqual(server.captured['data'].get('tools',[]),[])
     self.assertIn('image',json.dumps(server.captured['data']))
     if provider=='anthropic': self.assertTrue(server.captured['path'].startswith('/v1/messages'))
     else: self.assertEqual(server.captured['path'],'/v1/responses')
  finally:
   server.shutdown();server.server_close()

if __name__=='__main__': unittest.main()
