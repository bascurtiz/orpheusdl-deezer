import re
from enum import Enum
from urllib.parse import urlparse
from requests import get
from utils.models import *
from utils.utils import create_temp_filename
from .dzapi import DeezerAPI


module_information = ModuleInformation(
    service_name = 'Deezer',
    module_supported_modes = ModuleModes.download | ModuleModes.lyrics | ModuleModes.covers | ModuleModes.credits,
    global_settings = {'client_id': '447462', 'client_secret': 'a83bf7f38ad2f137e444727cfc3775cf', 'bf_secret': 'g4el58wc0zvf9na1'},
    session_settings = {'email': '', 'password': '', 'arl': '', 'use_arl': 'false'},
    session_storage_variables = ['arl'],
    netlocation_constant = ['deezer', 'dzr'],
    url_decoding = ManualEnum.manual,
    test_url = 'https://www.deezer.com/track/3135556',
)

class ImageType(Enum):
    cover = auto(),
    artist = auto(),
    playlist = auto(),
    user = auto(),
    misc = auto(),
    talk = auto()

class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.settings = module_controller.module_settings
        self.exception = module_controller.module_error
        self.tsc = module_controller.temporary_settings_controller
        self.default_cover = module_controller.orpheus_options.default_cover_options
        self.disable_subscription_check = module_controller.orpheus_options.disable_subscription_check
        if self.default_cover.file_type is ImageFileTypeEnum.webp:
            self.default_cover.file_type = ImageFileTypeEnum.jpg

        self.session = DeezerAPI(
            self.exception,
            self.settings.get('client_id', '447462'),
            self.settings.get('client_secret', 'a83bf7f38ad2f137e444727cfc3775cf'),
            self.settings.get('bf_secret', '')
        )
        arl = module_controller.temporary_settings_controller.read('arl') or (self.settings.get('arl') or '').strip()
        if arl:
            try:
                self.session.login_via_arl(arl)
                self.tsc.set('arl', arl)
            except self.exception:
                em = (self.settings.get('email') or '').strip()
                pw = (self.settings.get('password') or '').strip()
                if em and pw:
                    self.login(em, pw)
                else:
                    raise

        self.quality_parse = {
            QualityEnum.MINIMUM: 'MP3_128',
            QualityEnum.LOW: 'MP3_128',
            QualityEnum.MEDIUM: 'MP3_320',
            QualityEnum.HIGH: 'MP3_320',
            QualityEnum.LOSSLESS: 'FLAC',
            QualityEnum.HIFI: 'FLAC'
        }
        self.format = self.quality_parse[module_controller.orpheus_options.quality_tier]
        self.compression_nums = {
            CoverCompressionEnum.high: 80,
            CoverCompressionEnum.low: 50
        }
        if arl:
            self.check_sub()

    def _ensure_credentials(self):
        """Require valid credentials before download/metadata. Without this, we would fail with
        AttributeError (e.g. missing language) or only get previews. Matches Spotify/Qobuz: show
        what's missing and where to fill it in."""
        if getattr(self.session, 'api_token', None):
            return
        arl = self.tsc.read('arl') or (self.settings.get('arl') or '').strip()
        email = (self.settings.get('email') or '').strip()
        password = (self.settings.get('password') or '').strip()
        if arl:
            try:
                self.session.login_via_arl(arl)
                self.tsc.set('arl', arl)
                self.check_sub()
            except self.exception:
                if email and password:
                    self.login(email, password)
                else:
                    raise
            return
        if email and password:
            self.login(email, password)
            return
        error_msg = (
            'Deezer credentials are missing in settings.json. '
            'Please fill in either email and password, or arl. '
            'Use the OrpheusDL GUI Settings tab (Deezer) or edit config/settings.json directly.'
        )
        raise self.exception(error_msg)

    def login(self, email: str, password: str):
        arl_from_settings = (self.settings.get('arl') or '').strip()
        if arl_from_settings:
            self.session.login_via_arl(arl_from_settings)
            self.tsc.set('arl', arl_from_settings)
            self.check_sub()
            return True
        if not (email and password):
            raise self.exception(
                'Deezer credentials are required. Please fill in your email and password in the settings. '
                'Alternatively, you can use ARL instead.'
            )
        arl, _ = self.session.login_via_email(email, password)
        self.tsc.set('arl', arl)
        self.check_sub()

    def custom_url_parse(self, link):
        url = urlparse(link)

        if url.hostname == 'dzr.page.link':
            r = get('https://dzr.page.link' + url.path, allow_redirects=False)
            if r.status_code != 302:
                raise self.exception(f'Invalid URL: {link}')
            url = urlparse(r.headers['Location'])

        path_match = re.match(r'^\/(?:[a-z]{2}\/)?(track|album|artist|playlist)\/(\d+)\/?$', url.path)
        if not path_match:
            raise self.exception(f'Invalid URL: {link}')

        return MediaIdentification(
            media_type = DownloadTypeEnum[path_match.group(1)],
            media_id = path_match.group(2)
        )

    def get_track_info(self, track_id: str, quality_tier: QualityEnum, codec_options: CodecOptions, data={}, alb_tags={}) -> TrackInfo:
        self._ensure_credentials()
        is_user_upped = int(track_id) < 0
        format = self.quality_parse[quality_tier] if not is_user_upped else 'MP3_MISC'

        track = None
        if data and track_id in data:
            track = data[track_id]
        elif not is_user_upped:
            track = self.session.get_track(track_id)
        else:   # user-upped tracks can't be requested with deezer.pageTrack
            track = self.session.get_track_data(track_id)

        t_data = track
        if not is_user_upped:
            t_data = t_data['DATA']
        if 'FALLBACK' in t_data:
            t_data = t_data['FALLBACK']

        tags = Tags(
            track_number = t_data.get('TRACK_NUMBER'),
            copyright = t_data.get('COPYRIGHT'),
            isrc = t_data['ISRC'],
            disc_number = t_data.get('DISK_NUMBER'),
            replay_gain = t_data.get('GAIN'),
            release_date = t_data.get('PHYSICAL_RELEASE_DATE'),
        )

        for key in alb_tags:
            setattr(tags, key, alb_tags[key])

        error = None
        if is_user_upped:
            if not t_data['RIGHTS']['STREAM_ADS_AVAILABLE']:
                error = 'Cannot download track uploaded by another user'
        else:
            premium_formats = ['FLAC', 'MP3_320']
            countries = t_data['AVAILABLE_COUNTRIES']['STREAM_ADS']
            if not countries:
                error = 'Track not available'
            elif self.session.country not in countries:
                error = 'Track not available in your country'
            else:
                formats_to_check = premium_formats
                while len(formats_to_check) != 0:
                    if formats_to_check[0] != format:
                        formats_to_check.pop(0)
                    else:
                        break

                temp_f = None
                for f in formats_to_check:
                    if t_data[f'FILESIZE_{f}'] != '0':
                        temp_f = f
                        break
                if temp_f is None:
                    temp_f = 'MP3_128'
                format = temp_f

                if format not in self.session.available_formats:
                    error = 'Format not available by your subscription'

        codec = {
            'MP3_MISC': CodecEnum.MP3,
            'MP3_128': CodecEnum.MP3,
            'MP3_320': CodecEnum.MP3,
            'FLAC': CodecEnum.FLAC,
        }[format]

        bitrate = {
            'MP3_MISC': None,
            'MP3_128': 128,
            'MP3_320': 320,
            'FLAC': 1411,
        }[format]

        download_extra_kwargs = {
            'id': t_data['SNG_ID'],
            'track_token': t_data['TRACK_TOKEN'],
            'track_token_expiry': t_data['TRACK_TOKEN_EXPIRE'],
            'format': format,
        }

        duration_sec = None
        if t_data.get('DURATION') is not None:
            try:
                duration_sec = int(t_data['DURATION'])
            except (TypeError, ValueError):
                pass
        return TrackInfo(
            name = t_data['SNG_TITLE'] if not t_data.get('VERSION') else f'{t_data["SNG_TITLE"]} {t_data["VERSION"]}',
            album_id = t_data['ALB_ID'],
            album = t_data['ALB_TITLE'],
            artists = [a['ART_NAME'] for a in t_data['ARTISTS']] if 'ARTISTS' in t_data else [t_data['ART_NAME']],
            tags = tags,
            codec = codec,
            cover_url = self.get_image_url(t_data['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, self.default_cover.resolution, self.compression_nums[self.default_cover.compression]),
            release_year = int(tags.release_date.split('-')[0]) if tags.release_date else None,
            duration = duration_sec,
            explicit = t_data['EXPLICIT_LYRICS'] == '1' if 'EXPLICIT_LYRICS' in t_data else None,
            artist_id = t_data['ART_ID'],
            bit_depth = 16,
            sample_rate = 44.1,
            bitrate = bitrate,
            download_extra_kwargs = download_extra_kwargs,
            cover_extra_kwargs = {'data': {track_id: t_data['ALB_PICTURE']}},
            credits_extra_kwargs = {'data': {track_id: t_data.get('SNG_CONTRIBUTORS')}},
            lyrics_extra_kwargs = {'data': {track_id: track.get('LYRICS')}},
            error = error
        )

    def get_track_download(self, id, track_token, track_token_expiry, format):
        path = create_temp_filename()

        url = self.session.get_track_url(id, track_token, track_token_expiry, format)

        self.session.dl_track(id, url, path)

        return TrackDownloadInfo(
            download_type = DownloadEnum.TEMP_FILE_PATH,
            temp_file_path = path
        )

    def get_album_info(self, album_id: str, data={}) -> Optional[AlbumInfo]:
        self._ensure_credentials()
        album = data[album_id] if album_id in data else self.session.get_album(album_id)
        a_data = album['DATA']

        # placeholder images can't be requested as pngs
        cover_type = self.default_cover.file_type if a_data['ALB_PICTURE'] != '' else ImageFileTypeEnum.jpg

        tracks_data = album['SONGS']['data']
        try:
            total_tracks = int(tracks_data[-1]['TRACK_NUMBER'])
            total_discs = int(tracks_data[-1]['DISK_NUMBER'])
        except IndexError:
            total_tracks = 0
            total_discs = 0

        alb_tags = {
            'total_tracks': total_tracks,
            'total_discs': total_discs,
            'upc': a_data['UPC'],
            'label': a_data['LABEL_NAME'],
            'album_artist': a_data['ART_NAME'],
            'release_date': a_data.get('ORIGINAL_RELEASE_DATE') or a_data['PHYSICAL_RELEASE_DATE']
        }

        # Deezer album SONGS have reduced schema (no TRACK_TOKEN, etc.); get_track_info needs full pageTrack data, so do not pass track data here
        return AlbumInfo(
            name = a_data['ALB_TITLE'],
            artist = a_data['ART_NAME'],
            tracks = [track['SNG_ID'] for track in tracks_data],
            release_year = alb_tags['release_date'].split('-')[0],
            explicit = a_data['EXPLICIT_ALBUM_CONTENT']['EXPLICIT_LYRICS_STATUS'] in (1, 4),
            artist_id = a_data['ART_ID'],
            cover_url = self.get_image_url(a_data['ALB_PICTURE'], ImageType.cover, cover_type, self.default_cover.resolution, self.compression_nums[self.default_cover.compression]),
            cover_type = cover_type,
            all_track_cover_jpg_url = self.get_image_url(a_data['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, self.default_cover.resolution, self.compression_nums[self.default_cover.compression]),
            track_extra_kwargs = {'alb_tags': alb_tags},
        )

    def get_playlist_info(self, playlist_id: str, data={}) -> PlaylistInfo:
        self._ensure_credentials()
        playlist = data[playlist_id] if playlist_id in data else self.session.get_playlist(playlist_id, -1, 0)
        p_data = playlist['DATA']
        songs = playlist.get('SONGS', {}).get('data') or []

        # Prefer public API for cover: it returns the proper 2x2 composite; internal pagePlaylist often returns a placeholder.
        cover_url = self.session.get_playlist_cover_public(playlist_id)
        p_pic = (p_data.get('PLAYLIST_PICTURE') or '').strip()
        if not cover_url and p_pic:
            cover_type = self.default_cover.file_type if p_pic else ImageFileTypeEnum.jpg
            cover_url = self.get_image_url(p_pic, ImageType.playlist, cover_type, self.default_cover.resolution, self.compression_nums[self.default_cover.compression])
        if not cover_url and songs and isinstance(songs[0], dict) and songs[0].get('ALB_PICTURE'):
            cover_url = self.get_image_url(songs[0]['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, self.default_cover.resolution, self.compression_nums[self.default_cover.compression])

        # placeholder images can't be requested as pngs
        cover_type = self.default_cover.file_type if p_pic else ImageFileTypeEnum.jpg

        # Only user-uploaded tracks (SNG_ID < 0) go in data; they can't be fetched via pageTrack. Normal tracks need full pageTrack data from API.
        user_upped_dict = {}
        for t in songs:
            if int(t['SNG_ID']) < 0:
                user_upped_dict[t['SNG_ID']] = t

        return PlaylistInfo(
            name = p_data['TITLE'],
            creator = p_data['PARENT_USERNAME'],
            tracks = [t['SNG_ID'] for t in songs],
            release_year = p_data['DATE_ADD'].split('-')[0],
            creator_id = p_data['PARENT_USER_ID'],
            cover_url = cover_url,
            cover_type = cover_type,
            description = p_data['DESCRIPTION'],
            track_extra_kwargs = {'data': user_upped_dict}
        )

    def get_artist_info(self, artist_id: str, get_credited_albums: bool, artist_name = None) -> ArtistInfo:
        self._ensure_credentials()
        name = artist_name if artist_name else self.session.get_artist_name(artist_id)
        discography = self.session.get_artist_discography(artist_id, 0, -1, get_credited_albums)
        albums_out = []
        for a in discography:
            if not isinstance(a, dict):
                albums_out.append(str(a) if a is not None else '')
                continue
            data = a.get('DATA', a)
            title = data.get('ALB_TITLE') if isinstance(data, dict) else None
            if title is not None:
                release_date = (data.get('ORIGINAL_RELEASE_DATE') or data.get('PHYSICAL_RELEASE_DATE') or '') if isinstance(data, dict) else ''
                release_year = release_date.split('-')[0] if release_date else None
                cover_url = ''
                pic = data.get('ALB_PICTURE') if isinstance(data, dict) else None
                if pic:
                    cover_url = self.get_image_url(pic, ImageType.cover, ImageFileTypeEnum.jpg, 56, 80)
                albums_out.append({
                    'id': data.get('ALB_ID', a.get('ALB_ID', '')),
                    'name': title,
                    'artist': data.get('ART_NAME', name) if isinstance(data, dict) else name,
                    'release_year': release_year,
                    'cover_url': cover_url,
                })
            else:
                albums_out.append(data.get('ALB_ID', a.get('ALB_ID', a)) if isinstance(data, dict) else a.get('ALB_ID', a))
        return ArtistInfo(
            name = name,
            albums = albums_out if albums_out else self.session.get_artist_album_ids(artist_id, 0, -1, get_credited_albums),
        )

    def get_track_credits(self, track_id: str, data={}):
        if int(track_id) < 0:
            return []

        credits = data[track_id] if track_id in data else self.session.get_track_contributors(track_id)
        if not credits:
            return []

        # fixes tagging conflict with normal artist tag, it's redundant anyways
        credits.pop('artist', None)

        return [CreditsInfo(k, v) for k, v in credits.items()]

    def get_track_cover(self, track_id: str, cover_options: CoverOptions, data={}) -> CoverInfo:
        cover_md5 = data[track_id] if track_id in data else self.session.get_track_cover(track_id)

        # placeholder images can't be requested as pngs
        file_type = cover_options.file_type if cover_md5 != '' and cover_options.file_type is not ImageFileTypeEnum.webp else ImageFileTypeEnum.jpg

        url = self.get_image_url(cover_md5, ImageType.cover, file_type, cover_options.resolution, self.compression_nums[cover_options.compression])
        return CoverInfo(url=url, file_type=file_type)

    def get_track_lyrics(self, track_id: str, data={}) -> LyricsInfo:
        if int(track_id) < 0:
            return LyricsInfo()

        try:
            lyrics = data[track_id] if track_id in data else self.session.get_track_lyrics(track_id)
        except self.exception:
            return LyricsInfo()
        if not lyrics:
            return LyricsInfo()

        synced_text = None
        if 'LYRICS_SYNC_JSON' in lyrics:
            synced_text = ''
            for line in lyrics['LYRICS_SYNC_JSON']:
                if 'lrc_timestamp' in line:
                    synced_text += f'{line["lrc_timestamp"]}{line["line"]}\n'
                else:
                    synced_text += '\n'

        return LyricsInfo(embedded=lyrics['LYRICS_TEXT'], synced=synced_text)

    def search(self, query_type: DownloadTypeEnum, query: str, track_info: TrackInfo = None, limit: int = 10):
        self._ensure_credentials()

        results = {}
        if track_info and track_info.tags.isrc:
            results = [self.session.get_track_data_by_isrc(track_info.tags.isrc)]
        if not results:
            results = self.session.search(query, query_type.name, 0, limit)['data']

        if query_type is DownloadTypeEnum.track:
            search_results = []
            
            # Fetch preview URLs from public API for all tracks at once
            track_ids = [i['SNG_ID'] for i in results]
            public_data = self.session.get_tracks_public_data(track_ids)
            
            for i in results:
                track_id = str(i['SNG_ID'])
                
                # Get preview URL from public API data
                preview_url = None
                if track_id in public_data:
                    preview_url = public_data[track_id].get('preview')
                
                # Get cover image URL (small thumbnail for search results)
                cover_url = None
                if i.get('ALB_PICTURE'):
                    cover_url = self.get_image_url(i['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, 56, 80)
                # Fallback to public API cover if internal API doesn't have it
                elif track_id in public_data and public_data[track_id].get('album_cover_small'):
                    cover_url = public_data[track_id].get('album_cover_small')
                
                search_results.append(SearchResult(
                    result_id = i['SNG_ID'],
                    name = i['SNG_TITLE'] if not i.get('VERSION') else f'{i["SNG_TITLE"]} {i["VERSION"]}',
                    artists = [a['ART_NAME'] for a in i['ARTISTS']],
                    explicit = i['EXPLICIT_LYRICS'] == '1',
                    duration = int(i['DURATION']) if i.get('DURATION') else None,
                    image_url = cover_url,
                    preview_url = preview_url,
                    additional = [i["ALB_TITLE"]]
                ))
            return search_results
        elif query_type is DownloadTypeEnum.album:
            search_results = []
            for i in results:
                cover_url = None
                if i.get('ALB_PICTURE'):
                    cover_url = self.get_image_url(i['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, 56, 80)
                search_results.append(SearchResult(
                    result_id = i['ALB_ID'],
                    name = i['ALB_TITLE'],
                    artists = [a['ART_NAME'] for a in i['ARTISTS']],
                    year = i['PHYSICAL_RELEASE_DATE'].split('-')[0],
                    explicit = i['EXPLICIT_ALBUM_CONTENT']['EXPLICIT_LYRICS_STATUS'] in (1, 4),
                    image_url = cover_url,
                    additional = [f"1 track" if i['NUMBER_TRACK'] == 1 else f"{i['NUMBER_TRACK']} tracks"]
                ))
            return search_results
        elif query_type is DownloadTypeEnum.artist:
            search_results = []
            for i in results:
                # Artist picture
                cover_url = None
                if i.get('ART_PICTURE'):
                    cover_url = self.get_image_url(i['ART_PICTURE'], ImageType.artist, ImageFileTypeEnum.jpg, 56, 80)
                search_results.append(SearchResult(
                    result_id = i['ART_ID'],
                    name = i['ART_NAME'],
                    image_url = cover_url,
                    extra_kwargs = {'artist_name': i['ART_NAME']}
                ))
            return search_results
        elif query_type is DownloadTypeEnum.playlist:
            search_results = []
            for idx, i in enumerate(results):
                if not i.get('NB_SONG'):
                    continue
                cover_url = None
                if i.get('PLAYLIST_PICTURE'):
                    cover_url = self.get_image_url(i['PLAYLIST_PICTURE'], ImageType.playlist, ImageFileTypeEnum.jpg, 56, 80)
                # Deezer search/internal API often return a placeholder. Deemix uses public API (playlist/id) for proper composite cover. Prefer it for first 25.
                if idx < 25:
                    try:
                        public_cover = self.session.get_playlist_cover_public(i['PLAYLIST_ID'])
                        if public_cover:
                            cover_url = public_cover
                        if not cover_url:
                            full = self.session.get_playlist(i['PLAYLIST_ID'], 4, 0)
                            p_data = full.get('DATA') if isinstance(full.get('DATA'), dict) else None
                            p_pic = (p_data.get('PLAYLIST_PICTURE') or '').strip() if p_data else ''
                            songs = (full.get('SONGS') or {}).get('data') or []
                            if p_pic:
                                cover_url = self.get_image_url(p_pic, ImageType.playlist, ImageFileTypeEnum.jpg, 56, 80)
                            elif songs and isinstance(songs[0], dict) and songs[0].get('ALB_PICTURE'):
                                cover_url = self.get_image_url(songs[0]['ALB_PICTURE'], ImageType.cover, ImageFileTypeEnum.jpg, 56, 80)
                    except Exception:
                        pass
                search_results.append(SearchResult(
                    result_id = i['PLAYLIST_ID'],
                    name = i['TITLE'],
                    artists = [i['PARENT_USERNAME']],
                    image_url = cover_url,
                    additional = [f"1 track" if i['NB_SONG'] == 1 else f"{i['NB_SONG']} tracks"]
                ))
            return search_results

    def get_image_url(self, md5, img_type: ImageType, file_type: ImageFileTypeEnum, res, compression):
        if res > 3000:
            res = 3000

        filename = {
            ImageFileTypeEnum.jpg: f'{res}x0-000000-{compression}-0-0.jpg',
            ImageFileTypeEnum.png: f'{res}x0-none-100-0-0.png'
        }[file_type]

        return f'https://cdn-images.dzcdn.net/images/{img_type.name}/{md5}/{filename}'

    def check_sub(self):
        if not self.disable_subscription_check and (self.format not in self.session.available_formats):
            print('Deezer: quality set in the settings is not accessible by the current subscription')
