using System;
using System.IO;
using System.Text.RegularExpressions;

namespace YingXu.Desktop
{
    internal static class QuickVideoResource
    {
        internal static bool Supports(string path)
        {
            switch(Path.GetExtension(path).ToLowerInvariant()) {
                case ".mp4":case ".m4v":case ".mov":case ".webm":case ".mkv":case ".avi":return true;
                default:return false;
            }
        }
        internal static string Mime(string path)
        {
            switch(Path.GetExtension(path).ToLowerInvariant()) {
                case ".mp4":case ".m4v":return "video/mp4";
                case ".mov":return "video/quicktime";case ".webm":return "video/webm";case ".mkv":return "video/x-matroska";
                case ".avi":return "video/x-msvideo";case ".wmv":return "video/x-ms-wmv";case ".ts":return "video/mp2t";default:return "video/mpeg";
            }
        }
        internal static bool Range(string header,long length,out long start,out long end)
        {
            start=0;end=length-1;
            if(String.IsNullOrEmpty(header))return true;
            var match=Regex.Match(header,@"^bytes=(\d*)-(\d*)$",RegexOptions.CultureInvariant);
            if(!match.Success || length<=0)return false;
            long first,last;
            if(match.Groups[1].Value.Length==0) {
                if(!Int64.TryParse(match.Groups[2].Value,out last)||last<=0)return false;
                start=Math.Max(0,length-last);return true;
            }
            if(!Int64.TryParse(match.Groups[1].Value,out first)||first>=length)return false;
            start=first;
            if(match.Groups[2].Value.Length>0) {
                if(!Int64.TryParse(match.Groups[2].Value,out last)||last<first)return false;
                end=Math.Min(last,length-1);
            }
            return true;
        }
    }

    // One handle per reader pins the exact source through rename/replacement and
    // prevents writes while decoding. Range wrappers never own additional handles.
    internal sealed class QuickVideoFile : IDisposable
    {
        private readonly FileStream file;
        private bool disposed;
        internal QuickVideoFile(string path) { file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read); }
        internal long Length { get { lock(this) { return file.Length; } } }
        internal int Read(long position,byte[] buffer,int offset,int count)
        {
            lock(this) {
                if(disposed)throw new ObjectDisposedException("QuickVideoFile");
                file.Seek(position,SeekOrigin.Begin);return file.Read(buffer,offset,count);
            }
        }
        public void Dispose() { lock(this) { if(!disposed){disposed=true;file.Dispose();} } }
    }

    internal sealed class QuickVideoStream : Stream
    {
        private readonly QuickVideoFile file;
        private readonly long start,length;
        private readonly Func<bool> revoked;
        private long position;
        private bool disposed;
        internal QuickVideoStream(QuickVideoFile source,long first,long last,Func<bool> isRevoked)
        {
            file=source;start=first;length=Math.Max(0,last-first+1);revoked=isRevoked;
        }
        public override bool CanRead { get { return !disposed; } }
        public override bool CanSeek { get { return !disposed; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }
        public override long Position { get { return position; } set { Seek(value,SeekOrigin.Begin); } }
        public override int Read(byte[] buffer,int offset,int count)
        {
            lock(this) {
                if(disposed||revoked())throw new ObjectDisposedException("QuickVideoStream");
                if(buffer==null)throw new ArgumentNullException("buffer");
                if(offset<0||count<0||offset>buffer.Length-count)throw new ArgumentOutOfRangeException();
                if(position>=length)return 0;
                int read=file.Read(start+position,buffer,offset,(int)Math.Min(count,length-position));position+=read;return read;
            }
        }
        public override long Seek(long offset,SeekOrigin origin)
        {
            lock(this) {
                if(origin!=SeekOrigin.Begin && origin!=SeekOrigin.Current && origin!=SeekOrigin.End)throw new ArgumentOutOfRangeException("origin");
                long target=checked((origin==SeekOrigin.Begin?0:origin==SeekOrigin.Current?position:length)+offset);
                if(target<0||target>length)throw new IOException("Seek outside selected range.");position=target;return position;
            }
        }
        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer,int offset,int count) { throw new NotSupportedException(); }
        protected override void Dispose(bool disposing) { disposed=true;base.Dispose(disposing); }
    }
}
