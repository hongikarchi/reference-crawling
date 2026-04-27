import http.server
import socketserver
import sqlite3
import json
import urllib.parse
import os

PORT = 8081
DB_PATH = 'data/crawl/divisare.db'

class DivisareHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                
                c.execute("SELECT COUNT(*) FROM divisare_projects")
                total_projects = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM divisare_architects")
                total_architects = c.fetchone()[0]
                
                # Check pending status
                try:
                    c.execute("SELECT COUNT(*) FROM pending_projects WHERE status='pending'")
                    pending_projects = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM pending_projects WHERE status='done'")
                    done_projects = c.fetchone()[0]
                except sqlite3.OperationalError:
                    # Tables might not exist if crawler hasn't started fully
                    pending_projects = 0
                    done_projects = 0
                
                conn.close()
                
                res = {
                    "total_projects": total_projects,
                    "total_architects": total_architects,
                    "pending_projects": pending_projects,
                    "done_projects": done_projects
                }
                self.wfile.write(json.dumps(res).encode('utf-8'))
            except Exception as e:
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
                
        elif parsed_path.path == '/api/projects':
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            
            query = urllib.parse.parse_qs(parsed_path.query)
            offset = int(query.get('offset', ['0'])[0])
            limit = int(query.get('limit', ['60'])[0])
            
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                
                c.execute('''
                    SELECT id, slug, name, architect_names, location_country, location_city, 
                           project_year, area_sqm, abstract, tag_slugs, cover_image_url
                    FROM divisare_projects
                    ORDER BY fetched_at DESC NULLS LAST, id DESC
                    LIMIT ? OFFSET ?
                ''', (limit, offset))
                
                rows = [dict(row) for row in c.fetchall()]
                
                for row in rows:
                    if row['architect_names']:
                        try: row['architect_names'] = json.loads(row['architect_names'])
                        except: pass
                    if row['tag_slugs']:
                        try: row['tag_slugs'] = json.loads(row['tag_slugs'])
                        except: pass
                        
                conn.close()
                self.wfile.write(json.dumps(rows).encode('utf-8'))
            except Exception as e:
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            super().do_GET()

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # Allow restarting server quickly on same port
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), DivisareHandler) as httpd:
        print(f"Divisare monitoring server running on port {PORT}")
        httpd.serve_forever()
