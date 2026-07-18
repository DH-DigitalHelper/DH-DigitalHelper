//! The single writer/coordinator.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};

use rusqlite::{Connection, TransactionBehavior};
use tokio::sync::mpsc::UnboundedReceiver;
use tokio::sync::oneshot;

use crate::outcome::Outcome;
use crate::progress::ProgressSink;
use crate::storage::{self, FrontierItem, LinkEdge, QueueInsert};

const BATCH_MAX: usize = 64;
const PROGRESS_INTERVAL_MS: u128 = 100;

/// How a fetched URL's own queue row should be updated.
#[derive(Debug)]
pub enum UrlMark {
    Checked {
        http_status: i64,
        etag: Option<String>,
        last_modified: Option<String>,
        content_sha256: Option<String>,
        changed: bool,
        present: bool,
    },
    Removed {
        http_status: i64,
    },
    Error {
        http_status: Option<i64>,
    },
}

/// A followable in-domain link discovered on a page.
#[derive(Debug)]
pub struct FollowCandidate {
    pub url: String,
    pub depth: i64,
}

#[derive(Debug)]
pub struct RawDocUpsert {
    pub sha: String,
    pub source_type: String,
    pub raw_path: String,
    pub bytes: i64,
}

/// The crawl_log row for this fetch (run_id + site are filled by the coordinator).
#[derive(Debug)]
pub struct CrawlLogRow {
    pub final_url: String,
    pub status: Option<i64>,
    pub content_type: Option<String>,
    pub sha256: Option<String>,
    pub bytes: i64,
    pub kind: Option<String>,
    pub error: Option<String>,
}

/// One finished page's complete set of writes.
#[derive(Debug)]
pub struct PageBatch {
    pub site_idx: usize,
    pub url: String,
    pub now: String,
    pub mark: UrlMark,
    pub followable: Vec<FollowCandidate>,
    pub edges: Vec<LinkEdge>,
    pub raw_doc: Option<RawDocUpsert>,
    pub log: CrawlLogRow,
    pub outcome: Outcome,
}

pub enum WriterMsg {
    Claim {
        site_idx: usize,
        reply: oneshot::Sender<ClaimResult>,
    },
    Complete(Box<PageBatch>),
}

/// Reply to a `Claim`: fetch this URL, or the site is finished.
pub enum ClaimResult {
    Give(FrontierItem),
    Done,
}

/// Per-site running counts.
#[derive(Debug, Clone, Default)]
pub struct Counts {
    pub fetched: i64,
    pub new: i64,
    pub changed: i64,
    pub unchanged: i64,
    pub removed: i64,
    pub error: i64,
    pub skipped: i64,
}

impl Counts {
    fn record(&mut self, outcome: Outcome) {
        self.fetched += 1;
        match outcome {
            Outcome::New => self.new += 1,
            Outcome::Changed => self.changed += 1,
            Outcome::Unchanged => self.unchanged += 1,
            Outcome::Removed => self.removed += 1,
            Outcome::Error => self.error += 1,
            Outcome::Skipped => self.skipped += 1,
        }
    }

    pub fn pairs(&self) -> [(&'static str, i64); 7] {
        [
            ("fetched", self.fetched),
            ("new", self.new),
            ("changed", self.changed),
            ("unchanged", self.unchanged),
            ("removed", self.removed),
            ("error", self.error),
            ("skipped", self.skipped),
        ]
    }
}

/// Frontier heap element ordered so `BinaryHeap::pop` yields the smallest `(depth, url)` — i.e. breadth-first, matching the old `ORDER BY depth, url`.
struct HeapItem(FrontierItem);

impl PartialEq for HeapItem {
    fn eq(&self, other: &Self) -> bool {
        self.0.depth == other.0.depth && self.0.url == other.0.url
    }
}
impl Eq for HeapItem {}
impl Ord for HeapItem {
    fn cmp(&self, other: &Self) -> Ordering {
        (other.0.depth, &other.0.url).cmp(&(self.0.depth, &self.0.url))
    }
}
impl PartialOrd for HeapItem {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

struct SiteState {
    name: String,
    max_pages: i64,
    max_pages_per_host: i64,
    heap: BinaryHeap<HeapItem>,
    seen: HashSet<String>,
    in_flight: usize,
    given: i64,
    host_given: HashMap<String, i64>,
    host_over_budget_dropped: i64,
    waiters: VecDeque<oneshot::Sender<ClaimResult>>,
    counts: Counts,
    last_current: String,
    dirty: bool,
}

/// The lowercased host of `url`, or `None` if it doesn't parse / has no host.
fn host_of(url: &str) -> Option<String> {
    url::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_ascii_lowercase()))
}

impl SiteState {
    fn budget_spent(&self) -> bool {
        self.max_pages > 0 && self.given >= self.max_pages
    }

    /// Try to satisfy a claim from current state without parking.
    fn try_serve(&mut self) -> Option<ClaimResult> {
        if self.budget_spent() {
            return Some(ClaimResult::Done);
        }
        while let Some(item) = self.heap.pop() {
            if self.max_pages_per_host > 0 {
                let host = host_of(&item.0.url).unwrap_or_default();
                if *self.host_given.get(&host).unwrap_or(&0) >= self.max_pages_per_host {
                    self.host_over_budget_dropped += 1;
                    continue;
                }
                *self.host_given.entry(host).or_insert(0) += 1;
            }
            self.given += 1;
            self.in_flight += 1;
            return Some(ClaimResult::Give(item.0));
        }
        if self.in_flight == 0 {
            return Some(ClaimResult::Done);
        }
        None
    }

    /// Hand `res` to a claimant, undoing `try_serve`'s bookkeeping if the claimant is already gone.
    fn send_or_return(&mut self, reply: oneshot::Sender<ClaimResult>, res: ClaimResult) {
        let Err(returned) = reply.send(res) else {
            return;
        };
        if let ClaimResult::Give(item) = returned {
            self.in_flight = self.in_flight.saturating_sub(1);
            self.given -= 1;
            if self.max_pages_per_host > 0
                && let Some(n) = self
                    .host_given
                    .get_mut(&host_of(&item.url).unwrap_or_default())
            {
                *n -= 1;
            }
            self.heap.push(HeapItem(item));
        }
    }

    /// Serve as many parked waiters as current state allows.
    fn service_waiters(&mut self) {
        while let Some(front) = self.waiters.front() {
            if front.is_closed() {
                self.waiters.pop_front();
                continue;
            }
            match self.try_serve() {
                Some(res) => {
                    let reply = self.waiters.pop_front().unwrap();
                    self.send_or_return(reply, res);
                }
                None => break,
            }
        }
    }
}

/// Per-site frontier seed handed to the coordinator at startup.
pub struct SiteInit {
    pub name: String,
    pub max_pages: i64,
    pub max_pages_per_host: i64,
    pub frontier: Vec<FrontierItem>,
    pub seen: Vec<String>,
}

pub struct Coordinator {
    conn: Connection,
    run_id: String,
    sites: Vec<SiteState>,
    progress: ProgressSink,
    last_paint: Option<std::time::Instant>,
    url_ids: storage::UrlInterner,
}

impl Coordinator {
    pub fn new(
        conn: Connection,
        run_id: String,
        inits: Vec<SiteInit>,
        progress: ProgressSink,
    ) -> Self {
        let sites = inits
            .into_iter()
            .map(|init| {
                let mut heap = BinaryHeap::new();
                for item in init.frontier {
                    heap.push(HeapItem(item));
                }
                SiteState {
                    name: init.name,
                    max_pages: init.max_pages,
                    max_pages_per_host: init.max_pages_per_host,
                    heap,
                    seen: init.seen.into_iter().collect(),
                    in_flight: 0,
                    given: 0,
                    host_given: HashMap::new(),
                    host_over_budget_dropped: 0,
                    waiters: VecDeque::new(),
                    counts: Counts::default(),
                    last_current: String::new(),
                    dirty: false,
                }
            })
            .collect();
        Self {
            conn,
            run_id,
            sites,
            progress,
            last_paint: None,
            url_ids: storage::UrlInterner::new(),
        }
    }

    /// Run the coordinator loop until the channel closes (all workers gone).
    pub fn run(
        mut self,
        mut rx: UnboundedReceiver<WriterMsg>,
    ) -> rusqlite::Result<HashMap<String, Counts>> {
        while let Some(first) = rx.blocking_recv() {
            match first {
                WriterMsg::Claim { site_idx, reply } => self.handle_claim(site_idx, reply),
                WriterMsg::Complete(batch) => {
                    let mut completes: Vec<Box<PageBatch>> = vec![batch];
                    let mut deferred: Option<(usize, oneshot::Sender<ClaimResult>)> = None;
                    while completes.len() < BATCH_MAX {
                        match rx.try_recv() {
                            Ok(WriterMsg::Complete(b)) => completes.push(b),
                            Ok(WriterMsg::Claim { site_idx, reply }) => {
                                deferred = Some((site_idx, reply));
                                break;
                            }
                            Err(_) => break,
                        }
                    }
                    self.apply_completes(completes)?;
                    if let Some((site_idx, reply)) = deferred {
                        self.handle_claim(site_idx, reply);
                    }
                }
            }
        }
        storage::checkpoint_truncate(&self.conn);
        for s in &self.sites {
            if s.host_over_budget_dropped > 0 {
                self.progress.summary(
                    &format!("{} per-host budget reached", s.name),
                    &[
                        ("skipped_over_budget", s.host_over_budget_dropped),
                        ("max_pages_per_host", s.max_pages_per_host),
                    ],
                );
            }
        }
        let map = self
            .sites
            .iter()
            .map(|s| (s.name.clone(), s.counts.clone()))
            .collect();
        Ok(map)
    }

    fn handle_claim(&mut self, site_idx: usize, reply: oneshot::Sender<ClaimResult>) {
        let site = &mut self.sites[site_idx];
        match site.try_serve() {
            Some(res) => site.send_or_return(reply, res),
            None => site.waiters.push_back(reply),
        }
    }

    /// Apply up to `BATCH_MAX` finished pages in ONE transaction.
    #[allow(clippy::vec_box)]
    fn apply_completes(&mut self, completes: Vec<Box<PageBatch>>) -> rusqlite::Result<()> {
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        {
            let c: &Connection = &tx;
            let interner = &mut self.url_ids;
            for b in &completes {
                let site_name = self.sites[b.site_idx].name.clone();
                apply_one(c, interner, &self.run_id, &site_name, b)?;
            }
        }
        tx.commit()?;

        let mut touched: Vec<usize> = Vec::new();
        for b in &completes {
            let idx = b.site_idx;
            let site = &mut self.sites[idx];
            site.in_flight = site.in_flight.saturating_sub(1);
            site.counts.record(b.outcome);
            site.last_current = b.url.clone();
            site.dirty = true;
            for cand in &b.followable {
                if site.seen.insert(cand.url.clone()) {
                    site.heap.push(HeapItem(FrontierItem {
                        url: cand.url.clone(),
                        depth: cand.depth,
                        etag: None,
                        last_modified: None,
                        content_sha256: None,
                        present: true,
                    }));
                }
            }
            if !touched.contains(&idx) {
                touched.push(idx);
            }
        }
        for idx in touched {
            self.sites[idx].service_waiters();
        }
        self.maybe_emit_progress(false);
        Ok(())
    }

    fn maybe_emit_progress(&mut self, force: bool) {
        let now = std::time::Instant::now();
        if !force
            && let Some(last) = self.last_paint
            && now.duration_since(last).as_millis() < PROGRESS_INTERVAL_MS
        {
            return;
        }
        self.last_paint = Some(now);
        let updates: Vec<crate::progress::SiteUpdate> = self
            .sites
            .iter_mut()
            .filter(|s| s.dirty)
            .map(|s| {
                s.dirty = false;
                crate::progress::SiteUpdate {
                    key: s.name.clone(),
                    counts: s.counts.pairs(),
                    current: s.last_current.clone(),
                    queued: s.heap.len() as i64,
                }
            })
            .collect();
        if !updates.is_empty() {
            self.progress.update(&updates);
        }
    }
}

/// Apply one page's writes within an open transaction `c`.
fn apply_one(
    c: &Connection,
    interner: &mut storage::UrlInterner,
    run_id: &str,
    site: &str,
    b: &PageBatch,
) -> rusqlite::Result<()> {
    match &b.mark {
        UrlMark::Checked {
            http_status,
            etag,
            last_modified,
            content_sha256,
            changed,
            present,
        } => {
            storage::mark_url_checked(
                c,
                &b.url,
                *http_status,
                etag.as_deref(),
                last_modified.as_deref(),
                content_sha256.as_deref(),
                *changed,
                *present,
                &b.now,
            )?;
        }
        UrlMark::Removed { http_status } => {
            storage::mark_url_removed(c, &b.url, *http_status, &b.now)?;
            storage::mark_document_removed(c, &b.url, &b.now)?;
        }
        UrlMark::Error { http_status } => {
            storage::mark_url_error(c, &b.url, *http_status, &b.now)?;
        }
    }

    if !b.followable.is_empty() {
        let rows: Vec<QueueInsert> = b
            .followable
            .iter()
            .map(|cand| QueueInsert {
                url: cand.url.clone(),
                site: site.to_string(),
                depth: cand.depth,
                discovered_from: b.url.clone(),
                first_seen_at: b.now.clone(),
            })
            .collect();
        storage::enqueue_many(c, &rows)?;
    }
    if !b.edges.is_empty() {
        storage::insert_links(c, interner, &b.edges)?;
    }
    if let Some(rd) = &b.raw_doc {
        let is_new =
            storage::upsert_raw_doc(c, &rd.sha, &rd.source_type, &rd.raw_path, rd.bytes, &b.now)?;
        if !is_new {
            storage::requeue_extraction(c, &rd.sha)?;
        }
    }
    storage::record_fetch(
        c,
        run_id,
        &b.url,
        &b.log.final_url,
        site,
        b.log.status,
        b.log.content_type.as_deref(),
        b.log.sha256.as_deref(),
        b.log.bytes,
        b.log.kind.as_deref(),
        b.outcome.as_str(),
        b.log.error.as_deref(),
        &b.now,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::FrontierItem;

    fn site_with_one_url(url: &str) -> SiteState {
        let mut heap = BinaryHeap::new();
        heap.push(HeapItem(FrontierItem {
            url: url.to_string(),
            depth: 0,
            etag: None,
            last_modified: None,
            content_sha256: None,
            present: true,
        }));
        SiteState {
            name: "x.de".into(),
            max_pages: 0,
            max_pages_per_host: 2,
            heap,
            seen: HashSet::new(),
            in_flight: 0,
            given: 0,
            host_given: HashMap::new(),
            host_over_budget_dropped: 0,
            waiters: VecDeque::new(),
            counts: Counts::default(),
            last_current: String::new(),
            dirty: false,
        }
    }

    /// try_serve commits the bookkeeping and pops the URL before the reply is sent, so a worker that died in between takes the URL with it and leaves in_flight incremented forever.
    #[test]
    fn a_lost_claim_is_fully_returned_to_the_frontier() {
        let mut site = site_with_one_url("https://x.de/a");
        let (tx, rx) = oneshot::channel();
        drop(rx);

        let res = site.try_serve().expect("one URL is available");
        assert_eq!(
            site.in_flight, 1,
            "precondition: try_serve commits up front"
        );

        site.send_or_return(tx, res);

        assert_eq!(
            site.in_flight, 0,
            "in_flight must not leak, or the site hangs"
        );
        assert_eq!(site.given, 0, "a lost claim must not spend the page budget");
        assert_eq!(
            site.host_given.get("x.de").copied().unwrap_or(0),
            0,
            "nor the per-host budget"
        );
        assert_eq!(site.heap.len(), 1, "the URL must go back on the frontier");
    }

    /// The happy path must keep committing, or every claim would be undone.
    #[test]
    fn a_delivered_claim_keeps_its_bookkeeping() {
        let mut site = site_with_one_url("https://x.de/a");
        let (tx, rx) = oneshot::channel();

        let res = site.try_serve().expect("one URL is available");
        site.send_or_return(tx, res);

        assert_eq!(site.in_flight, 1);
        assert_eq!(site.given, 1);
        assert_eq!(site.heap.len(), 0);
        assert!(matches!(rx.blocking_recv(), Ok(ClaimResult::Give(_))));
    }
}
