# gtfs-jp-monitor

🇹🇷 **Türkçe** · 🇬🇧 [English](README.md) · 🇯🇵 [日本語](README.ja.md)

[gtfs-data.jp](https://gtfs-data.jp) üzerinde yayımlanan GTFS feed'leri için izleme hattı.

**Web sitesi: <https://ttezer.github.io/gtfs-jp-monitor/>** (her gün güncellenir; Türkçe, İngilizce, Japonca)

Hat, her feed yayını için (kalıcı `gtfs_file_uid` kimliğiyle) [GTFS Analyzer](https://github.com/ttezer/gtfs-analyzer)'ı
çalıştırır, dilden bağımsız kısa bir özet saklar ve yayınlar arasındaki farkları üretir: doğrulama
farkları ve semantic değişiklik raporları (duraklar, hatlar, güzergâhlar, sefer saatleri, hizmet
günleri, ücretler).

Durum: her gün çalışıyor; geliştirme sürüyor. Hattın sözleşmeleri
[docs/data-model.md](docs/data-model.md) içinde anlatılır (İngilizce).

Sonuçlar her gün <https://ttezer.github.io/gtfs-jp-monitor/> adresinde yayımlanır. Bir karşılaştırma
bağlantısıyla paylaşılabilir (data-model §10.1); her semantic rapor ayrıca
`reports/<org_id>/<feed_id>/<old_uid>__<new_uid>.json.gz` adresinde JSON olarak yayımlanır
(data-model §10.0) ve `status.json` sitenin en son ne zaman güncellendiğini gösterir.

Her yayın çifti anlamlı hizmet değişikliği, yalnız teknik değişiklik, eşdeğer ya da bilinmiyor
olarak sınıflandırılır; raporu olmayan çiftler, hangi dosyaların değiştiğine bakılarak tahmin edilir
(data-model §12). `metrics.json` feed başına ve toplamda son 365 günü verir: yayın ve anlamlı
güncelleme sıklığı, değişiklik oranları, doğrulama gerilemeleri ve kapsam (§13). `fields.json.gz`,
tekerlekli sandalye bilgisi gibi seçilmiş isteğe bağlı alanların ne kadar dolu olduğunu verir ve
Japonya dışında kalan ya da koordinatları yer değiştirmiş durakları işaretler (§15).

## Nasıl çalışır

Bir GitHub Actions workflow'u (`.github/workflows/gtfs-jp-monitor.yml`) her gün 04:10 JST'de çalışır
(GitHub daha geç başlatabilir): kataloğu eşitler, yeni yayınları analiz eder, semantic raporları
üretir, sonuçları ayrı ve private bir veri reposuna commit eder ve siteyi GitHub Pages'e yayımlar.
Veri reposundaki bir watchdog her akşam sitenin son 30 saat içinde yeniden kurulduğunu kontrol
eder ve GitHub günlük koşuyu etkinliksizlik nedeniyle kapattıysa onu yeniden açar
(data-model §10, §10.2). Kaynakta aynı kimlikle dosyası değiştirilen bir yayın yeniden analiz edilir
ve raporları yeniden üretilir (data-model §3.3).

## Kendin çalıştırmak

Python 3.11 veya üstü; hat standart kütüphane dışında bir şeye ihtiyaç duymaz. Komutlar bir veri
reposu kopyası olan `--data-dir` üzerinde çalışır (`install-analyzer` `--dest` alır); her birinin
`--help` seçeneği vardır:

| Komut | Yaptığı |
|---|---|
| `sync-catalog` | gtfs-data.jp'den feed ve yayın kataloğunu çeker |
| `install-analyzer` | Sabitlenmiş GTFS Analyzer sürümünü indirir ve doğrular (`analyzer.lock.json`) |
| `analyze` | Analyzer ve profil için henüz kaydı olmayan yayınları analiz eder |
| `semantic-reports` | Sayfada görünen yayınların eksik değişiklik raporlarını üretir |
| `export-web` | Veri klasöründen siteyi kurar (`--html`) |
| `semantic-report`, `rawdiff` | İki GTFS ZIP dosyasını doğrudan karşılaştırır |

```bash
PYTHONPATH=src python3 -m gtfs_jp_monitor sync-catalog --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor install-analyzer --dest analyzer
PYTHONPATH=src python3 -m gtfs_jp_monitor analyze --data-dir data --analyzer analyzer/gtfs-analyzer --limit 20
PYTHONPATH=src python3 -m gtfs_jp_monitor semantic-reports --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor export-web --data-dir data --release-tag v0.15.0 --html site/index.html
```

Yerel olarak derlenmiş bir Analyzer'ın sonuçları yalnız "scratch" olarak işaretlenmiş bir klasöre
yazılır (data-model §6.2); yayımlanan veri workflow'dan gelir.

## Dizin yapısı

| Yol | İçerik |
|---|---|
| `docs/` | Hattın sözleşmeleri (`data-model.md`) ve semantic motor belirtimleri |
| `schemas/` | Üretilen verinin JSON şemaları |
| `src/gtfs_jp_monitor/` | Hattın kodu |
| `src/gtfs_jp_semantic/` | Semantic değişiklik motoru (`docs/semantic/`) |
| `web/prototype/` | `export-web` ile doldurulan web sayfası |
| `tests/` | Birim testleri |

Testleri çalıştırmak için (şema testleri `dev` ekini, yani `jsonschema`'yı ister):

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
```

Üretilen veri ayrı bir veri reposuna yazılır; ham GTFS ZIP dosyaları hiç saklanmaz, yalnız her
birinin içerik imzası saklanır (data-model §4.2).

API istemcisi yalnız Python standart kütüphanesini kullanır ve TLS sertifikalarını her zaman
doğrular. Sistem CA sertifikalarıyla gelmeyen Python kurulumları (örneğin python.org'un macOS
yükleyicisi) bir CA paketi ister, örneğin `SSL_CERT_FILE=$(python3 -m certifi)`.

## Veri kaynakları ve lisanslar

Feed verisi gtfs-data.jp'den ve her ulaşım işletmecisinden gelir. Her yayın kaydı, feed'i için
yayımlanan lisansı saklar; türetilmiş veriyi yeniden dağıtırken o lisansa uyun.

Bu reponun kodu [MIT Lisansı](LICENSE) ile yayımlanmıştır. Lisans feed verisini ve ondan türetilen
veriyi kapsamaz; onlar kendi lisanslarını korur.
