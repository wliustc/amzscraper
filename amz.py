import os
import re
import time
import random
import hashlib
import argparse
import memcache
import datetime
import mechanize
import subprocess

from BeautifulSoup import BeautifulSoup


class AmzScraper(object):
    headers = [
        ('User-agent', 'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.2.13) '
                       'Gecko/20101206 Ubuntu/10.10 (maverick) Firefox/3.6.13')
    ]
    login_url = 'https://www.amazon.com/gp/sign-in.html'
    start_url = 'https://www.amazon.com/gp/css/history/orders/view.html?orderFilter=year-{yr}&startAtIndex=1000'  # noqa
    page_url = 'https://www.amazon.com/gp/your-account/order-history/ref=oh_aui_pagination_{op}_{dp}?ie=UTF8&orderFilter=year-{yr}&search=&startIndex={idx}'  # noqa
    order_url = 'https://www.amazon.com/gp/css/summary/print.html/ref=od_aui_print_invoice?ie=UTF8&orderID={oid}'  # noqa

    pages_re = re.compile(r'href="\/gp\/your-account\/order-history\/.+pagination_\d+_(\d+).+?"')
    order_id_re = re.compile(r'href=".+?orderID=([0-9-]+)"')
    order_date_re = re.compile(r'Order Placed:.+?([a-zA-Z]+ \d{1,2}, \d{4})')

    def __init__(self, email, password, year, orders_dir, cache_timeout):
        self.email = email
        self.password = password
        self.year = year
        self.orders_dir = orders_dir
        self.cache_timeout = cache_timeout
        self.mc = memcache.Client(['127.0.0.1:11211'], debug=0)
        self.br = mechanize.Browser()
        self.br.set_handle_robots(False)
        self.br.addheaders = self.headers
        self.login()

    def login(self):
        self.br.open('https://www.amazon.com')
        self.br.follow_link(text_regex='Sign in')
        self.br.select_form(nr=0)
        self.br['email'] = self.email
        self.br['password'] = self.password
        resp = self.br.submit()
        if resp.code != 200:
            raise Exception('Got invalid response code %s' % resp.code)
        elif resp.geturl().startswith('https://www.amazon.com/ap/signin'):  # not the URL we wanted
            html = resp.get_data()
            soup = BeautifulSoup(html)
            err = soup.findAll('div', attrs={'id': 'message_error'})
            msg = (err and err[0].renderContents() or html).strip()
            raise Exception('Login failed for %s, %s: %s' % (self.email, self.password, msg))

    def _fetch_url(self, url, use_cache=True):
        key = hashlib.md5(url).hexdigest()
        val = use_cache and self.mc.get(key) or None
        if not val:
            print 'fetching %s from server (with random sleep)' % url
            for x in range(3):
                resp = self.br.open(url)
                if resp.geturl() == url:
                    break
                print 'got unexpected URL (%s); expecting %s. attempting re-login...'\
                      '' % (resp.geturl(), url)
                self.login()
            else:
                raise Exception('Got an unexpected URL (most recently, %s) 3 times. Expected URL: '
                                '%s' % (resp.geturl(), url))
            val = resp.get_data()
            # wait a little while so we don't spam Amazon
            time.sleep(random.randint(1, 5))
            self.mc.set(key, val, self.cache_timeout)
            from_cache = False
        else:
            print 'using cache for %s' % url
            from_cache = True
        return val, from_cache

    def get_page_nums(self):
        orders_html, _ = self._fetch_url(self.start_url.format(yr=self.year))
        # retrieve a list of the page numbers linked from this page (may not be complete)
        page_nums = [int(x) for x in re.findall(self.pages_re, orders_html)]
        if page_nums:
            # add the missing pages
            page_nums = range(min(page_nums), max(page_nums) + 1)
        else:
            # no page links were found, so there's only one page (which may or may not have orders)
            page_nums = [1]
        print 'found pages: %s' % page_nums
        return page_nums

    def get_order_nums(self, page_nums):
        order_nums = set()
        page_nums = list(page_nums)
        while page_nums:
            page_num = page_nums.pop(0)
            idx = (page_num - 1) * 10  # 10 items per page
            url = self.page_url.format(op=page_num-1, dp=page_num, yr=self.year, idx=idx)
            html, _ = self._fetch_url(url)
            order_nums |= set(re.findall(self.order_id_re, html))
        print 'found %s orders in %s' % (len(order_nums), self.year)
        return order_nums

    def run(self):
        page_nums = self.get_page_nums()
        order_nums = self.get_order_nums(page_nums)
        for oid in order_nums:
            orders = os.listdir(self.orders_dir)
            if any(['{oid}.pdf'.format(oid=oid) in o for o in orders]):
                print 'skipping order %s (already exists)' % oid
                continue
            url = self.order_url.format(oid=oid)
            html, from_cache = self._fetch_url(url)
            # force a re-fetch if we got a non-final order from the cache:
            if 'Final Details for Order #' not in html and from_cache:
                html, _ = self._fetch_url(url, use_cache=False)
            if 'Final Details for Order #' not in html:
                print 'skipping order %s (not final)' % oid
                continue
            date = re.findall(self.order_date_re, html.replace('\n', ' '))[0].strip()
            date = datetime.datetime.strptime(date, '%B %d, %Y').strftime('%Y-%m-%d')
            fn = 'amazon_order_{date}_{oid}.'.format(date=date, oid=oid) + '{ext}'
            fn = os.path.join(self.orders_dir, fn)
            with open(fn.format(ext='html'), 'w') as f:
                f.write(html)
            subprocess.check_call(['wkhtmltopdf', '--no-images', '--disable-javascript',
                                   fn.format(ext='html'), fn.format(ext='pdf')])
            os.remove(fn.format(ext='html'))


def parse_args():
    parser = argparse.ArgumentParser(description='Scrape an Amazon account and create order PDFs.')
    parser.add_argument('-u', '--user', required=True, help='Amazon.com username (email).')
    parser.add_argument('-p', '--password', required=True, help='Amazon.com password.')
    parser.add_argument('--cache-timeout', required=False, default=21600,
                        help='Timeout for URL caching, in seconds. Defaults to 6 hours.')
    parser.add_argument('--dest-dir', required=False, default='orders/',
                        help='Destination directory for scraped order PDFs. Defaults to "orders/"')
    parser.add_argument('year', nargs='*', type=int, default=datetime.datetime.today().year,
                        help='One or more years for which to retrieve orders. Will default to the '
                        'current year if no year is specified.')
    return parser.parse_args()


def main():
    args = parse_args()
    for year in args.year:
        AmzScraper(args.user, args.password, year=year, cache_timeout=args.cache_timeout,
                   orders_dir=args.dest_dir).run()

if __name__ == "__main__":
    main()
