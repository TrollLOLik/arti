"""Explicit local export of a bounded retained result on the bot's computer.

Uses that installation's database access. No provider/Telegram call. The output
is a user-specified destination, never a restored path from a request payload.
"""
import argparse
import asyncio
import os
from pathlib import Path
import shutil


async def export(pool,request_id,owner,output,*,disk=None):
    from bot.media_retention import Retention
    from bot.media_spool import MediaSpool
    async with pool.acquire() as conn:
        row=await conn.fetchrow('SELECT chat_id,topic_id FROM arti_requests WHERE id=$1',request_id)
    if row is None: raise ValueError('result_unavailable')
    retained=await Retention(pool).load(request_id,'result',owner,row['chat_id'],row['topic_id'])
    if retained is None: raise ValueError('result_unavailable')
    disk=disk or MediaSpool()
    output=Path(output).expanduser().absolute()
    # Exclusive creation: never overwrite a user's file, including a symlink.
    with disk.open_verified(retained['descriptor']) as source:
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
        try:
            with os.fdopen(fd,'wb') as destination:
                shutil.copyfileobj(source,destination,1024*1024)
                destination.flush(); os.fsync(destination.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return output


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True)
    parser.add_argument('--owner',type=int,required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    from database import connection
    await connection.init_db()
    try:
        await export(connection._pool,args.request,args.owner,args.output)
        print('Result exported to the requested destination.')
    finally:
        await connection.close_db()


if __name__=='__main__':
    asyncio.run(main())
