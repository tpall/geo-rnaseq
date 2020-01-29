import os
import re
import gzip
import tarfile
import io
import scipy
import collections
import pandas as pd
import numpy as np
from scipy.stats import binom
import argparse


xls = re.compile("xls")
keep = "|".join(
    ["\." + i + "(.gz)?$" for i in "tab xlsx diff tsv xls csv txt rtf".split(" ")]
)
keep = re.compile(keep)
gse = re.compile("GSE\d+_")
pv_str = "p.{0,4}val"
pv = re.compile(pv_str)
adj = re.compile("adj|fdr|corr")
fields = ["Type", "Class", "Conversion", "pi0", "FDR_pval", "hist", "note"]
PValSum = collections.namedtuple("PValSum", fields, defaults=[np.nan] * 7)


def find_header(df, n=20):
    head = df.head(n)
    idx = 0
    for index, row in head.iterrows():
        if all([isinstance(i, str) for i in row]):
            idx = index
            break
    return idx


def read_csv(input, tar=None):
    input_name = input
    csv = input
    if isinstance(input, (tarfile.TarInfo)):
        input_name = gse.search(tar.name).group(0) + input.name.replace("/", "_")
        csv = io.StringIO(tar.extractfile(input).read().decode("unicode_escape"))
        input = tar.extractfile(input)
    r = pd.read_csv(
        csv, sep=None, engine="python", iterator=True, skiprows=20, nrows=1000
    )
    sep = r._engine.data.dialect.delimiter
    df = pd.read_csv(input, sep=sep, comment="#", encoding="unicode_escape")
    if all(["Unnamed" in i for i in list(df.columns)]):
        idx = find_header(df)
        if idx > 0:
            df = pd.read_csv(
                input, sep=sep, comment="#", skiprows=idx, encoding="unicode_escape"
            )
    return {os.path.basename(input_name): df}


def read_excel(input, tar=None):
    tabs = {}
    input_name = input
    if isinstance(input, (tarfile.TarInfo)):
        input_name = gse.search(tar.name).group(0) + input.name.replace("/", "_")
        input = tar.extractfile(input)
    if input_name.endswith(".gz"):
        with gzip.open(input) as gz:
            wb = pd.ExcelFile(gz)
    else:
        wb = pd.ExcelFile(input)
    sheets = wb.sheet_names
    for sheet in sheets:
        df = wb.parse(sheet, comment="#")
        if not df.empty:
            tabs.update({os.path.basename(input_name) + "-sheet-" + sheet: df})
    return tabs


def import_tar(path):
    out = {}
    with tarfile.open(path, "r:*") as tar:
        for member in tar:
            if member.isfile():
                if keep.search(member.name):
                    if not member.name.startswith("."):
                        try:
                            if xls.search(member.name):
                                out.update(read_excel(member, tar))
                            else:
                                out.update(read_csv(member, tar))
                        except Exception as e:
                            out.update(note(gse.search(tar.name).group(0) + member.name.replace("/", "_"), e))
    return out


def import_flat(path):
    out = {}
    try:
        if xls.search(path):
            out.update(read_excel(path))
        else:
            out.update(read_csv(path))
    except Exception as e:
        out.update(note(os.path.basename(path), e))
    return out


def filter_pvalue_tables(input, pv=None, adj=None):
    return {
        k: v
        for k, v in input.items()
        if any([bool(pv.search(i.lower()) and not adj.search(i.lower())) for i in v.columns])
    }


def summarise_pvalue_tables(df, var=["basemean", "value", "logcpm", "rpkm"]):
    df.columns = map(str.lower, df.columns)
    pvalues = df.filter(regex=pv_str).copy()
    pvalues.columns = ["pvalue"]
    for v in var:
        label = v
        if v is "value":
            v = "^value_\d"
            label = "fpkm"
        frames = df.filter(regex=v, axis=1)
        if not frames.empty:
            frames = frames.mean(axis=1, skipna=True)
            pvalues.loc[:, label] = frames
    return pvalues.dropna(subset=["pvalue"])


# https://stackoverflow.com/a/32681075/1657871
def rle(inarray):
    """ run length encoding. Partial credit to R rle function. 
            Multi datatype arrays catered for including non Numpy
            returns: tuple (runlengths, startpositions, values) """
    ia = np.asarray(inarray)  # force numpy
    n = len(ia)
    if n == 0:
        return (None, None, None)
    else:
        y = np.array(ia[1:] != ia[:-1])  # pairwise unequal (string safe)
        i = np.append(np.where(y), n - 1)  # must include last element posi
        z = np.diff(np.append(-1, i))  # run lengths
        p = np.cumsum(np.append(0, z))[:-1]  # positions
        return (z, p, ia[i])


def get_hist_class(counts, breaks, fdr):
    qc = binom.ppf(1 - 1 / breaks * fdr, sum(counts), 1 / breaks)
    counts_over_qc = counts > qc
    for i in counts_over_qc:
        if all(~counts_over_qc):
            Class = "uniform"
        elif not any(counts_over_qc[rle(counts_over_qc)[0][0] + 2 :]):
            Class = "anti-conservative"
        elif not any(np.flip(counts_over_qc)[rle(np.flip(counts_over_qc))[0][0] + 2 :]):
            Class = "conservative"
        else:
            Class = "other"
    return Class


# https://gdsctools.readthedocs.io/en/master/_modules/gdsctools/qvalue.html#QValue
def estimate_pi0(
    pv,
    lambdas=None,
    pi0=None,
    df=3,
    method="smoother",
    smooth_log_pi0=False,
    verbose=True,
):
    """Estimate pi0 based on the pvalues"""
    try:
        pv = np.array(pv)
    except:
        pv = pv.copy()
    assert pv.min() >= 0 and pv.max() <= 1, "p-values should be between 0 and 1"
    if lambdas is None:
        epsilon = 1e-8
        lambdas = scipy.arange(0, 0.9 + 1e-8, 0.05)
    if len(lambdas) > 1 and len(lambdas) < 4:
        raise ValueError(
            """if length of lambda greater than 1, you need at least 4 values"""
        )
    if len(lambdas) >= 1 and (min(lambdas) < 0 or max(lambdas) >= 1):
        raise ValueError("lambdas must be in the range[0, 1[")
    m = float(len(pv))

    pv = pv.ravel()  # flatten array
    if pi0 is not None:
        pass
    elif len(lambdas) == 1:
        pi0 = np.mean(pv >= lambdas[0]) / (1 - lambdas[0])
        pi0 = min(pi0, 1)
    else:
        # evaluate pi0 for different lambdas
        pi0 = [np.mean(pv >= this) / (1 - this) for this in lambdas]
        # in R
        # lambda = seq(0,0.09, 0.1)
        # pi0 = c(1.0000000, 0.9759067, 0.9674164, 0.9622673, 0.9573241,
        #         0.9573241 0.9558824, 0.9573241, 0.9544406, 0.9457901)
        # spi0 = smooth.spline(lambda, pi0, df=3, all.knots=F, spar=0)
        # predict(spi0, x=max(lambda))$y  --> 0.9457946
        # spi0 = smooth.spline(lambda, pi0, df=3, all.knots=F)
        # predict(spi0, x=max(lambda))$y  --> 0.9485383
        # In this function, using pi0 and lambdas, we get 0.9457946
        # this is not too bad, the difference on the v17 data set
        # is about 0.3 %
        if method == "smoother":
            if smooth_log_pi0:
                pi0 = np.log(pi0)
            # In R, the interpolation is done with smooth.spline
            # within qvalue. However this is done with default
            # parameters, and this is different from the Python
            # code. Note, however, that smooth.spline has a parameter
            # called spar. If set to 0, then we would get the same
            # as in scipy. It looks like scipy has no equivalent of
            # the smooth.spline function in R if spar is not 0
            tck = scipy.interpolate.splrep(lambdas, pi0, k=df)
            pi0 = scipy.interpolate.splev(lambdas[-1], tck)
            if smooth_log_pi0:
                pi0 = np.exp(pi0)
            pi0 = min(pi0, 1.0)
        elif method == "bootstrap":
            raise NotImplementedError
            """minpi0 = min(pi0)
            mse = rep(0, len(lambdas))
            pi0.boot = rep(0, len(lambdas))
            for i in range(1,100):
                p.boot = sample(p, size = m, replace = TRUE)
                for i in range(0,len(lambdas)):
                    pi0.boot[i] <- mean(p.boot > lambdas[i])/(1 - lambdas[i])
                mse = mse + (pi0.boot - minpi0)^2
            pi0 = min(pi0[mse == min(mse)])
            pi0 = min(pi0, 1)"""
        if pi0 > 1:
            if verbose:
                print("got pi0 > 1 (%.3f), setting it to 1" % pi0)
            pi0 = 1.0
    assert pi0 >= 0 and pi0 <= 1, "pi0 is not between 0 and 1: %f" % pi0
    return pi0


def conversion(x, y):
    classes = pd.DataFrame.from_dict(
        {
            "uniform": ["same good", "improve, effects", "worsen", "worsen"],
            "anti-conservative": ["effects lost", "same good", "worsen", "worsen"],
            "conservative": [
                "improvement, no effects",
                "improvement, effects",
                "same bad",
                "no improvement",
            ],
            "other": [
                "improvement, no effects",
                "improvement, effects",
                "no improvement",
                "same bad",
            ],
        },
        orient="index",
        columns=["uniform", "anti-conservative", "conservative", "other"],
    )
    return classes.loc[x, y]


def summarise_pvalues(
    df,
    breaks=30,
    fdr=0.05,
    var={"basemean": 10, "fpkm": 0.5, "logcpm": np.log2(0.5), "rpkm": 0.5},
):
    # Test if pvalues are in 0 to 1 range
    if not df.min()["pvalue"] >= 0 and df.max()["pvalue"] <= 1:
        return PValSum(note="p-values not in 0 to 1 range")
    bins = np.linspace(0, 1, breaks)
    center = (bins[:-1] + bins[1:]) / 2
    # Filter pvalues
    pf = pd.DataFrame()
    for k, v in var.items():
        if k in df.columns:
            pf = df.loc[df[k] >= v, ["pvalue"]]
            filt = k
            break
    # Make histogram
    pv_sets = [i for i in [df, pf] if not i.empty]
    hists = [np.histogram(i["pvalue"], bins=bins) for i in pv_sets]
    counts = [counts.tolist() for (counts, bins) in hists]
    # Test if p-values are truncated
    truncated = rle([i == 0 for i in counts[0]])[1][-1] > 0
    if truncated:
        return PValSum(note="p-values are truncated")
    # Assign class to histograms
    Class = [get_hist_class(i, breaks, fdr) for i in counts]
    # Conversion
    Type = ["raw"]
    conv = np.nan
    if len(Class) == 2:
        conv = conversion(Class[0], Class[1])
        Type = ["raw", filt]
    # Calculate pi0
    pi0 = []
    for i, c in zip(pv_sets, Class):
        if c in ["uniform", "anti-conservative"]:
            pi0_est = estimate_pi0(i["pvalue"])
            pi0.append(pi0_est.item())
        else:
            pi0.append(np.nan)
    # Number of effects < FDR
    fdr_effects = [sum(i["pvalue"] < fdr) for i in pv_sets]
    return PValSum(Type, Class, conv, pi0, fdr_effects, counts)


def write_to_csv(input, outpath):
    for k, v in input.items():
        v.to_csv(outpath + k + ".csv", sep=",", index=False)


def note(filename, text):
    return {filename: pd.DataFrame(PValSum(note=text)._asdict(), index=[0])}

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", nargs="+", help="path to input file to be parsed")
    group.add_argument(
        "--list",
        metavar="FILE",
        type=argparse.FileType("r"),
        help="file with paths to input files, one per line",
    )
    parser.add_argument(
        "out", type=argparse.FileType("w", encoding="UTF-8"), help="output file"
    )
    parser.add_argument(
        "--vars",
        metavar="KEY=VALUE",
        nargs="*",
        default=["basemean=10", "fpkm=0.5", "logcpm=-0.3", "rpkm=0.5"],
        help="variables for expression level filtering. Input 'key=value' pairs without spaces around equation mark",
    )
    parser.add_argument(
        "--breaks",
        type=int,
        default=30,
        help="number of histogram bins, integer, default is 30",
    )
    parser.add_argument(
        "--fdr",
        type=float,
        default=0.05,
        help="false discovery rate, float, default is 0.05",
    )
    args = parser.parse_args()
    var = dict(map(lambda s: s.split("="), args.vars))
    VAR = {k: float(v) for k, v in var.items()}
    BREAKS = args.breaks
    FDR = args.fdr

    if args.file:
        input = args.file
    elif args.list:
        input = []
        with args.list as f:
            for line in f:
                input.append(line.rstrip())

    out = {}
    for path in input:
        filename = os.path.basename(path)
        if path.endswith("tar.gz"):
            frames = import_tar(path)
        else:
            frames = import_flat(path)
        out.update({k: v for k, v in frames.items() if "note" in v.columns})
        frames = filter_pvalue_tables(frames, pv, adj)
        if len(frames) == 0:
            out.update(note(filename, "no pvalues"))
        else:
            frames = {
                k: summarise_pvalue_tables(v, var=VAR.keys()) for k, v in frames.items()
            }
            pv_stats = {
                k: summarise_pvalues(v, breaks=BREAKS, fdr=FDR, var=VAR)
                for k, v in frames.items()
            }
            for k, v in pv_stats.items():
                key = re.sub(r"^.*-(sheet-.*)", r"\1", k) + " from " + filename
                if k == filename:
                    key = k
                out.update({key: pd.DataFrame.from_dict(v._asdict())})

    # print(pd.DataFrame.from_dict({k: v._asdict() for k,v in out.items()}, orient="index").reset_index().rename(columns = {"index": "id"}))
    result = pd.concat(
        [df for df in out.values()], keys=[k for k in out.keys()], names=["id"]
    )
    print(result)
    result.reset_index(level="id").to_csv(args.out, index=False)