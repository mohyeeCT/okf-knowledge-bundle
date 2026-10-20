import gzip,json,tempfile,unittest
from pathlib import Path
from okf_bundle.cli import execute,parser
from okf_bundle.state import StateStore

class SitemapInventoryTests(unittest.TestCase):
    def test_duplicate_and_missing_loc_occurrences_survive_saved_merge_and_reimport(self):
        for compressed in [False,True]:
            with self.subTest(gzip=compressed),tempfile.TemporaryDirectory() as t:
                root=Path(t);run=root/'run';config=root/'config.json'
                config.write_text(json.dumps({'domains':['example.com'],'publication_owner':'human:fixture-owner'}))
                xml=b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a/</loc></url><url><loc>https://example.com/a/</loc></url><url/><url/></urlset>'
                source=root/('sitemap.xml.gz' if compressed else 'sitemap.xml');source.write_bytes(gzip.compress(xml) if compressed else xml)
                def call(*args):return execute(parser().parse_args([*args,'--run',str(run)]))
                call('init','--config',str(config))
                for _ in range(2):
                    call('discover-sitemap','--sitemap',str(source));state=StateStore(run).load()
                    self.assertEqual(4,len(state['inventory']))
                    self.assertEqual([1,2,3,4],[r['input_ref']['entry'] for r in state['inventory']])
                    self.assertEqual(['pending','duplicate','excluded','excluded'],[r['state'] for r in state['inventory']])
                    self.assertEqual(1,len(state['sources']))
                    self.assertEqual([],state['review_history']);self.assertEqual({},state['release_approvals'])

if __name__=='__main__':unittest.main()
