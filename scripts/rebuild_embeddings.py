"""Build, publish or restore a preserved SJFX embedding index candidate."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.embedding_rebuild import rebuild_embeddings, publish_rebuild


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',choices=['ordinary','large','mail'])
    parser.add_argument('--package-id')
    parser.add_argument('--db')
    parser.add_argument('--output-dir')
    parser.add_argument('--publish',action='store_true',help='Publish after candidate verification; old index stays in backup')
    parser.add_argument('--candidate-manifest',help='Publish an already built candidate')
    parser.add_argument('--restore-manifest',help='Restore only vector rows from a published rebuild backup')
    args=parser.parse_args()
    try:
        if args.restore_manifest: result=publish_rebuild(args.restore_manifest,restore=True)
        elif args.candidate_manifest: result=publish_rebuild(args.candidate_manifest)
        else:
            if not args.scope or not args.package_id: parser.error('--scope and --package-id are required')
            from config import Config
            db=args.db or (Config.DATA_DIR/'large_package.db' if args.scope=='large' else Config.DB_PATH)
            result=rebuild_embeddings(db,args.scope,args.package_id,publish=args.publish,output_dir=args.output_dir)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc),'old_index_preserved':True},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
