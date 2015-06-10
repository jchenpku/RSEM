__doc__="""

  pliu 20150511

  python module for a ChIP-seq experiment that contains
  replicates of ChIP-seq data for target and/or control
"""

import ChIPSeqReplicate
import File
import Util


class ChIPSeqExperiment:
  def __init__(self):
    self.param           = None ## reference to input parameters
    self.reps            = []   ## list of ChIPSeqReplciate object
    self.is_control      = None ## if is control
    self.pooled_tagalign = None ## File obj of pooled tagAlign
    self.peaks           = None ## File obj of targetRep0_VS_controlRep0 peaks
    self.final_peaks     = None ## File obj of final peaks


  @classmethod
  def initFromParam(cls, param, is_control, param_attr):
    cse = cls()
    cse.param = param
    cse.is_control = is_control
    ftgts = getattr(param, param_attr).split(',')
    cse.reps = [ ChIPSeqReplicate.initFromFastqFile(ffq) for ffq in ftgts ]
    for (i, rep) in enumerate(cse.reps):
      rep.param = param
      rep.index = i+1
      rep.chipseqexp = cse
      tgt_fta = "%s/%s.tagAlign.gz" % (param.temp_dir, rep.name)
      rep.tagalign = File.initFromFullFileName(tgt_fta)

    if cse.is_control:
      frep0 = param.temp_dir + 'controlRep0.tagAlign.gz'
    else:
      frep0 = param.temp_dir + 'targetRep0.tagAlign.gz'
    cse.pooled_tagalign = File.initFromFullFileName(frep0)

    rep0_basename = 'targetRep0_vs_controlRep0.regionPeak.gz'
    fpeaks    = param.temp_dir + rep0_basename
    cse.peaks = File.initFromFullFileName(fpeaks)

    cse.final_peaks = File.initFromFullFileName(param.fchipseq_peaks)

    return cse


  def getFastqEncoding(self):
    nthr = self.param.num_threads
    fin = ','.join([ f.fastq.fullname for f in self.reps])
    if self.is_control:
      fenc = self.param.imd_name + '_prsem.chipseq_control_encoding'
    else:
      fenc = self.param.imd_name + '_prsem.chipseq_target_encoding'

    Util.runCommand('/bin/env', 'Rscript', self.param.chipseq_rscript,
                    'guessFqEncoding', nthr, fin, fenc,
                    self.param.prsem_rlib_dir, quiet=self.param.quiet )

    with open(fenc, 'r') as f_fenc:
      next(f_fenc)
      file2enc = dict([ line.rstrip("\n").split("\t") for line in f_fenc ])

    for f in self.reps:
      f.encoding = file2enc[f.fastq.fullname]


  def alignReadByBowtie(self):
    if self.param.num_threads > 4:
      nthr_bowtie = self.param.num_threads - 4
    else:
      nthr_bowtie = 1

    bowtie_ref_name = "%s_prsem" % self.param.ref_name
    for rep in self.reps:
      cmd_cat = Util.getCatCommand(rep.fastq.is_gz)

      ## many pipes, have to use os.system
      cmds = [ "%s %s |" % (cmd_cat, rep.fastq.fullname) ] + \
             [ "%s -q -v 2 -a --best --strata -m 1 %s -S -p %d %s - |" % (
               self.param.bowtie_bin_for_chipseq, rep.encoding, nthr_bowtie,
               bowtie_ref_name ) ] + \
             [ "%s view -S -b -F 1548 - |" % self.param.samtools_bin ] + \
             [ "%s bamtobed -i stdin |" % (
               self.param.bedtools_bin_for_chipseq ) ] + \
             [ """awk 'BEGIN{FS="\\t";OFS="\\t"}{$4="N"; print $0}' |""" ] + \
             [ "gzip -c > %s " % rep.tagalign.fullname ]

      cmd = ' '.join(cmds)
      Util.runOneLineCommand(cmd, quiet=self.param.quiet)


  def poolTagAlign(self):
    import os
    frep0 = self.pooled_tagalign.fullname
    if os.path.exists(frep0):
      os.remove(frep0)
    for rep in self.reps:
      cat_cmd = Util.getCatCommand(rep.fastq.is_gz)
      cmd = "%s %s | gzip -c >> %s" % (cat_cmd, rep.tagalign.fullname, frep0)
      Util.runOneLineCommand(cmd, quiet=self.param.quiet)


  def callPeaksBySPP(self, ctrl_tagalign):
    """
    in principle, this function is only for ChIP-seq target experiment
    should make target and control inherit from ChIPSeqExperiment, will do
    """
    import sys
    import multiprocessing as mp
    if self.is_control:
      sys.exit( "ChIPSeqExperiment::runSPP() cann't be applied to control" )

    tgt_tagaligns = [self.pooled_tagalign] + [rep.tagalign for rep in self.reps]
    prm = self.param

    ## need to check and install spp ##
    Util.runCommand('/bin/env', 'Rscript', prm.chipseq_rscript,
                    'checkInstallSpp', prm.spp_tgz, prm.prsem_rlib_dir,
                    quiet=prm.quiet)

    nthr = prm.num_threads/len(tgt_tagaligns)
    fctrl_tagalign = ctrl_tagalign.fullname
    procs = [ mp.Process(target=runSPP, args=(tgt_tagalign, fctrl_tagalign,
                         prm, nthr)) for tgt_tagalign in tgt_tagaligns ]
    for p in procs:
      p.start()
    for p in procs:
      p.join()


  def getPeaksByIDR(self, ctrl_tagalign):
    """
    in principle, this function is only for ChIP-seq target experiment
    should make target and control inherit from ChIPSeqExperiment, will do
    """
    import sys
    import itertools
    import multiprocessing as mp
    if self.is_control:
      sys.exit( "ChIPSeqExperiment::runSPP() can't be applied to control" )

    procs = []
    out_q = mp.Queue()
    prm = self.param
    for (repa, repb) in itertools.combinations(self.reps, 2):
      fpeaka = prm.temp_dir + repa.tagalign.filename_sans_ext + '_VS_' + \
               ctrl_tagalign.filename_sans_ext + '.regionPeak.gz'
      fpeakb = prm.temp_dir + repb.tagalign.filename_sans_ext + '_VS_' + \
               ctrl_tagalign.filename_sans_ext + '.regionPeak.gz'
      idr_prefix = prm.temp_dir + 'idr_' + repa.tagalign.basename + '_vs_' + \
                   repb.tagalign.basename
      proc = mp.Process(target=getNPeaksByIDR,
                        args=(fpeaka, fpeakb, idr_prefix, prm, out_q))
      procs.append(proc)
      proc.start()

    fidr2npeaks = {}
    for p in procs:
      fidr2npeaks.update(out_q.get())
      p.join()

    max_npeaks = max(fidr2npeaks.values())
    cmd = 'zcat %s %s %s' % ( self.peaks.fullname,
            ' | sort -k7nr,8nr | head -n %d ' % max_npeaks,
            ' | gzip -c > %s ' % self.final_peaks.fullname)

    Util.runOneLineCommand(cmd, quiet=prm.quiet)


def getNPeaksByIDR(fpeaka, fpeakb, idr_prefix, prm, out_q):
  Util.runCommand('/bin/env', 'Rscript', prm.idr_script, fpeaka, fpeakb,
                  '-1', idr_prefix, '0', 'F', 'signal.value', prm.idr_scr_dir,
                  prm.fgenome_table, quiet=prm.quiet)
  fidr = idr_prefix + '-overlapped-peaks.txt'
  outdict = {}
  with open(fidr, 'r') as f_fidr:
    next(f_fidr)
    ## count the number of peaks w/ IDR <= IDR_THRESHOLD
    npk = sum( float(line.split()[10]) <= prm.IDR_THRESHOLD for line in f_fidr )
    outdict[fidr] = npk
    out_q.put(outdict)


def runSPP(tgt_tagalign, fctrl_tagalign, prm, nthr):
  import os
  spp_tmpdir = prm.temp_dir + tgt_tagalign.basename + '_spp_tmp/'
  if not os.path.exists(spp_tmpdir):
    os.mkdir(spp_tmpdir)
  fout = prm.temp_dir + tgt_tagalign.basename + '_phantom.tab'
  Util.runCommand('/bin/env', 'Rscript', prm.spp_script,
                  "-c=%s"      % tgt_tagalign.fullname,
                  "-i=%s"      % fctrl_tagalign,
                  "-npeak=%d"  % prm.N_PEAK,
                  prm.PEAK_TYPE,
                  '-savp',
                  "-x=%s"      % prm.EXCLUSION_ZONE,
                  '-rf',
                  "-odir=%s"   % prm.temp_dir,
                  "-p=%d"      % nthr,
                  "-tmpdir=%s" % spp_tmpdir,
                  "-out=%s"    % fout,
                  quiet=prm.quiet)
  os.rmdir(spp_tmpdir)


def initFromParam(param, typ):
  if typ.lower() == 'target':
    is_ctrl = False
    param_attr = 'chipseq_target_read_files'
  elif typ.lower() in [ 'control', 'input' ]:
    is_ctrl = True
    param_attr = 'chipseq_control_read_files'

  return ChIPSeqExperiment.initFromParam(param, is_ctrl, param_attr)
