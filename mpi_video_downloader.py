import json
import os
import time
import sys
from mpi4py import MPI
import yt_dlp

# Force UTF-8 output to avoid 'charmap' codec errors on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

def download_video(url, output_dir, quality='best', max_retries=2, progress_callback=None):
    """Downloads a single video using yt-dlp with retry logic."""
    
    for attempt in range(max_retries + 1):
        try:
            # More robust format to prevent "empty file" errors
            if quality == 'best':
                format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            elif quality == 'worst':
                format_str = 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[ext=mp4]/worst'
            elif quality in ['720p', '1080p', '480p']:
                height = quality.replace('p', '')
                format_str = f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best'
            else:
                format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            
            def ydl_hook(d):
                if progress_callback and d['status'] == 'downloading':
                    progress_callback(d)
            
            ydl_opts = {
                'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
                'format': format_str,
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': False,
                'no_color': True,
                'progress_hooks': [ydl_hook],
                # Enable fast parallel chunk downloading (speed boost)
                'nopart': False,
                'concurrent_fragment_downloads': 10,
                
                # Anti-bot bypass to fix "The downloaded file is empty"
                'extractor_args': {'youtube': ['player_client=android,web']},
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5'
                },
                'merge_output_format': 'mp4',
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise Exception("No info returned from yt-dlp — video may be unavailable.")
                return {
                    'status': 'success',
                    'url': url,
                    'title': info.get('title', 'Unknown'),
                    'file_path': ydl.prepare_filename(info),
                    'time': 0,  # Will be set by caller
                    'attempts': attempt + 1
                }
                
        except Exception as e:
            error_msg = str(e)
            
            if attempt < max_retries:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                return {
                    'status': 'error',
                    'url': url,
                    'title': 'Failed',
                    'error': error_msg,
                    'attempts': attempt + 1,
                    'time': 0
                }
    
    return {
        'status': 'error',
        'url': url,
        'error': 'Unknown error',
        'attempts': max_retries + 1,
        'time': 0
    }

def check_pause_flag(output_dir):
    """Check if pause flag exists."""
    pause_flag = os.path.join(output_dir, "pause.flag")
    return os.path.exists(pause_flag)

def write_status(status_file, status_data):
    """Write status to file with error handling using atomic rename."""
    try:
        temp_file = status_file + ".tmp"
        os.makedirs(os.path.dirname(status_file), exist_ok=True)
        
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(status_data, f, indent=2)
        
        # Atomic rename to avoid file locks from reader
        if os.path.exists(status_file):
            os.remove(status_file)
        os.rename(temp_file, status_file)
        
        return True
    except Exception as e:
        # Silently ignore - reader may have the file open
        return False

def main():
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()

    # Load Config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'config.json')
    
    if not os.path.exists(config_path):
        if rank == 0:
            print("ERROR: Config file not found.", file=sys.stderr)
        return

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        if rank == 0:
            print(f"ERROR: Failed to load config: {e}", file=sys.stderr)
        return

    # Extract config
    urls = config.get('urls', [])
    output_dir = os.path.abspath(os.path.join(script_dir, config.get('output_dir', 'downloads')))
    retry_failed = config.get('retry_failed', True)
    max_retries = config.get('max_retries', 2) if retry_failed else 0
    video_quality = config.get('video_quality', 'best')
    
    # Create output directory
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        print(f"Rank {rank}: Failed to create output directory '{output_dir}': {e}", file=sys.stderr)

    if rank == 0:
        print(f"[Master] Starting download of {len(urls)} URLs with {size} ranks")
        print(f"[Master] Quality: {video_quality}, Max retries: {max_retries}")

    # =========================================================
    # CORRECT WORK DISTRIBUTION:
    # Use a master-worker pattern with send/recv so each rank
    # gets a DIFFERENT URL to download. The bcast() approach
    # was wrong because it sent the SAME url to ALL ranks.
    # =========================================================

    # Status file for this rank
    status_file = os.path.join(output_dir, f'status_rank_{rank}.json')
    results = []

    try:
        if rank == 0:
            # --- MASTER: assigns work to workers ---
            url_queue = list(range(len(urls)))
            num_workers = size  # rank 0 also downloads

            # Track which ranks are still active
            active_workers = size - 1  # Excluding rank 0
            
            # Initial assignment: give each rank one URL to start
            next_url_ptr = 0
            
            # First, send an initial job to every rank (including self)
            initial_assignments = {}
            for r in range(size):
                if next_url_ptr < len(url_queue):
                    initial_assignments[r] = url_queue[next_url_ptr]
                    next_url_ptr += 1
                else:
                    initial_assignments[r] = -1  # No work for this rank

            # Send initial jobs to workers (rank 0 keeps its own)
            for r in range(1, size):
                comm.send(initial_assignments[r], dest=r, tag=0)
                if initial_assignments[r] == -1:
                    # Worker r receives -1 and terminates immediately without sending a done signal
                    active_workers -= 1

            # Rank 0 processes its own initial job
            url_index = initial_assignments[0]

            while True:
                if url_index == -1:
                    # Rank 0 has no more work, wait for all ACTIVE workers to finish
                    while active_workers > 0:
                        done_rank = comm.recv(source=MPI.ANY_SOURCE, tag=1)
                        if next_url_ptr < len(url_queue):
                            # Give worker another job
                            comm.send(url_queue[next_url_ptr], dest=done_rank, tag=0)
                            next_url_ptr += 1
                        else:
                            # Terminate worker
                            comm.send(-1, dest=done_rank, tag=0)
                            active_workers -= 1
                    break

                # Rank 0 downloads its assigned URL
                url = urls[url_index]
                
                # Check for pause
                while check_pause_flag(output_dir):
                    write_status(status_file, {
                        'rank': 0, 'current_url': url,
                        'completed': results, 'status': 'paused'
                    })
                    time.sleep(1)

                # Update status - downloading
                write_status(status_file, {
                    'rank': 0, 'current_url': url,
                    'completed': results, 'status': 'downloading',
                    'percent': 0.0, 'speed': '...', 'eta': '...',
                    'progress': f"{len(results)+1}/{len(urls)}"
                })
                
                print(f"[Rank 0] Starting download {len(results)+1}/{len(urls)}: {url}")

                last_write = [0.0]
                def master_progress_callback(d):
                    current_time = time.time()
                    if current_time - last_write[0] < 0.5:
                        return
                    last_write[0] = current_time
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes') or 0
                    percent = float((downloaded / total * 100) if total > 0 else 0.0)
                    speed = d.get('speed') or 0.0
                    eta = d.get('eta') or 0
                    speed_str = f"{speed/(1024*1024):.2f} MB/s" if speed > 1024*1024 else f"{speed/1024:.2f} KB/s" if speed > 1024 else f"{speed:.0f} B/s"
                    write_status(status_file, {
                        'rank': 0, 'current_url': url, 'completed': results,
                        'status': 'downloading', 'percent': percent,
                        'speed': speed_str, 'eta': f"{eta}s",
                        'progress': f"{len(results)+1}/{len(urls)}"
                    })

                start_time = time.time()
                res = download_video(url, output_dir, video_quality, max_retries, master_progress_callback)
                res['time'] = time.time() - start_time
                res['rank'] = 0
                results.append(res)

                status_str = "OK" if res['status'] == 'success' else "FAIL"
                print(f"[Rank 0] {status_str}: {res.get('title', url)} ({res['time']:.1f}s)")

                write_status(status_file, {
                    'rank': 0, 'current_url': None, 'completed': results,
                    'status': 'idle', 'percent': 100.0,
                    'progress': f"{len(results)}/{len(urls)}"
                })

                # Get next job from the queue
                if next_url_ptr < len(url_queue):
                    url_index = url_queue[next_url_ptr]
                    next_url_ptr += 1
                else:
                    url_index = -1
                    # Now wait for ACTIVE workers to finish and send them termination
                    while active_workers > 0:
                        done_rank = comm.recv(source=MPI.ANY_SOURCE, tag=1)
                        # If there's still work, send it; otherwise terminate
                        if next_url_ptr < len(url_queue):
                            comm.send(url_queue[next_url_ptr], dest=done_rank, tag=0)
                            next_url_ptr += 1
                        else:
                            comm.send(-1, dest=done_rank, tag=0)
                            active_workers -= 1
                    break

        else:
            # --- WORKER: receives jobs from master ---
            while True:
                url_index = comm.recv(source=0, tag=0)
                
                if url_index == -1:
                    # No more work
                    write_status(status_file, {
                        'rank': rank, 'current_url': None, 'completed': results,
                        'status': 'finished', 'percent': 100.0,
                    })
                    break

                url = urls[url_index]

                # Check for pause
                while check_pause_flag(output_dir):
                    write_status(status_file, {
                        'rank': rank, 'current_url': url,
                        'completed': results, 'status': 'paused'
                    })
                    time.sleep(1)

                # Update status - downloading
                write_status(status_file, {
                    'rank': rank, 'current_url': url,
                    'completed': results, 'status': 'downloading',
                    'percent': 0.0, 'speed': '...', 'eta': '...',
                    'progress': f"{len(results)+1}/{len(urls)}"
                })

                print(f"[Rank {rank}] Starting download {len(results)+1}: {url}")

                last_write = [0.0]
                def worker_progress_callback(d):
                    current_time = time.time()
                    if current_time - last_write[0] < 0.5:
                        return
                    last_write[0] = current_time
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes') or 0
                    percent = float((downloaded / total * 100) if total > 0 else 0.0)
                    speed = d.get('speed') or 0.0
                    eta = d.get('eta') or 0
                    speed_str = f"{speed/(1024*1024):.2f} MB/s" if speed > 1024*1024 else f"{speed/1024:.2f} KB/s" if speed > 1024 else f"{speed:.0f} B/s"
                    write_status(status_file, {
                        'rank': rank, 'current_url': url, 'completed': results,
                        'status': 'downloading', 'percent': percent,
                        'speed': speed_str, 'eta': f"{eta}s",
                        'progress': f"{len(results)+1}/{len(urls)}"
                    })

                start_time = time.time()
                res = download_video(url, output_dir, video_quality, max_retries, worker_progress_callback)
                res['time'] = time.time() - start_time
                res['rank'] = rank
                results.append(res)

                status_str = "OK" if res['status'] == 'success' else "FAIL"
                print(f"[Rank {rank}] {status_str}: {res.get('title', url)} ({res['time']:.1f}s)")

                write_status(status_file, {
                    'rank': rank, 'current_url': None, 'completed': results,
                    'status': 'idle', 'percent': 100.0,
                    'progress': f"{len(results)}/{len(urls)}"
                })

                time.sleep(0.2)

                # Signal master that this rank is done with its job
                comm.send(rank, dest=0, tag=1)

    except KeyboardInterrupt:
        print(f"[Rank {rank}] Interrupted by user", file=sys.stderr)
    except Exception as e:
        print(f"[Rank {rank}] Unexpected error: {e}", file=sys.stderr)
    finally:
        # Final status update
        write_status(status_file, {
            'rank': rank, 'current_url': None,
            'completed': results, 'status': 'finished'
        })
        
        # Synchronize all ranks and collect final stats
        try:
            comm.Barrier()
            
            if rank == 0:
                all_results = comm.gather(results, root=0)
                total_success = sum(sum(1 for r in rr if r['status'] == 'success') for rr in all_results)
                total_failed  = sum(sum(1 for r in rr if r['status'] == 'error')   for rr in all_results)
                
                print(f"\n[Master] ==================== SUMMARY ====================")
                print(f"[Master] Total URLs: {len(urls)}")
                print(f"[Master] Successful: {total_success}")
                print(f"[Master] Failed: {total_failed}")
                print(f"[Master] Downloads completed successfully!")
            else:
                comm.gather(results, root=0)
                
        except Exception as e:
            print(f"[Rank {rank}] Final sync error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()